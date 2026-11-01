#!/usr/bin/env bash
# Deploy the gate script (and optionally the routes) to the Hermes host, driven by config.json.
#
#   deploy/install.sh            # gate script + gate config only: live immediately, no restart
#   deploy/install.sh --routes   # also rewrite routes in Hermes config.yaml: needs a gateway restart
#
# With an "app" section in config.json it also installs the GitHub App token minter and its systemd timer.
# CONFIG=path/to/config.json overrides the default ./config.json.
# This never restarts the gateway: doing that from inside a Hermes session kills the session.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config.json}"
[[ -f "$CONFIG" ]] || { echo "No $CONFIG. Copy config.example.json to config.json and fill it in." >&2; exit 1; }

# read_cfg expands ~ (local paths like the ssh key); read_raw doesn't (paths on the remote host)
read_cfg() { python3 -c "import json,os,sys;c=json.load(open(sys.argv[2]));v=eval(sys.argv[1]);print(os.path.expanduser(v) if isinstance(v,str) else v)" "$1" "$CONFIG"; }
read_raw() { python3 -c "import json,sys;c=json.load(open(sys.argv[2]));print(eval(sys.argv[1]))" "$1" "$CONFIG"; }
HOST="$(read_cfg 'c["hermes"]["ssh_host"]')"
KEY="$(read_cfg 'c["hermes"]["ssh_key"]')"
PROFILES="$(read_cfg '" ".join(sorted({r["profile"] for r in c["routes"].values() if r.get("profile")}))')"
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")

python3 -m py_compile "$ROOT/gate/hermes_github_gate.py" "$ROOT/deploy/routes.py" "$ROOT/deploy/app_token.py" "$ROOT/deploy/app_setup.py"

# the gate only needs its own section plus identities
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 - "$CONFIG" "$TMP/hermes_github_gate.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
gate = {"bot_login": c["bot_login"], "bot_name": c.get("bot_name", "Hermes"), "owner_login": c["owner_login"], **c.get("gate", {})}
if c.get("mention"):
    gate["mention"] = c["mention"]
json.dump(gate, open(sys.argv[2], "w"), indent=2)
PY

scp -q -i "$KEY" "$ROOT/gate/hermes_github_gate.py" "$TMP/hermes_github_gate.json" "$ROOT/deploy/routes.py" "$CONFIG" "$HOST:/tmp/"
"${SSH[@]}" bash -s -- $PROFILES <<'REMOTE'
set -euo pipefail
S=/root/.hermes/scripts
install -d "$S"
install -m755 /tmp/hermes_github_gate.py "$S/hermes_github_gate.py"
install -m600 /tmp/hermes_github_gate.json "$S/hermes_github_gate.json"
for p in "$@"; do
  install -d "/root/.hermes/profiles/$p/scripts"
  # hardlinks, not symlinks: Hermes resolves symlinks and rejects scripts outside the profile's scripts/ dir
  ln -f "$S/hermes_github_gate.py" "/root/.hermes/profiles/$p/scripts/hermes_github_gate.py"
  ln -f "$S/hermes_github_gate.json" "/root/.hermes/profiles/$p/scripts/hermes_github_gate.json"
done
echo "gate installed for default profile${*:+ and: $*}"
REMOTE

# GitHub App mode: token minter + a timer that refreshes the token every 30 minutes (tokens live 60).
# Runs after the gate deploy, and a failed first refresh only warns, so a push problem never blocks gate deploys.
if [[ "$(read_raw 'bool(c.get("app", {}).get("dir"))')" == "True" ]]; then
  APP_DIR="$(read_raw 'c["app"]["dir"]')"
  # unit files are rendered here with systemd quoting, so no config value is ever re-parsed by a remote shell
  python3 - "$CONFIG" "$TMP" <<'PY'
import json, sys
c, out = json.load(open(sys.argv[1])), sys.argv[2]
app_dir = c["app"]["dir"]
token_file = c.get("gate", {}).get("token_file", "/root/.config/gh-bot/token")
for t in c["app"].get("push", []):
    host, _, path = t.partition(":")
    if not host or host.startswith("-") or not path.startswith("/"):
        sys.exit(f"app.push target {t!r} must look like user@host:/absolute/path")
def q(s):  # systemd ExecStart quoting: escape \ " % $
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
args = ["/usr/bin/python3", f"{app_dir}/app_token.py", "--app-dir", app_dir, "--out", token_file]
for t in c["app"].get("push", []):
    args += ["--push", t]
open(f"{out}/github-app-token.service", "w").write(f"""[Unit]
Description=Refresh the GitHub App installation token for the webhook bridge
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
TimeoutStartSec=3min
Restart=on-failure
RestartSec=60
ExecStart={' '.join(q(a) for a in args)}
""")
open(f"{out}/github-app-token.timer", "w").write("""[Unit]
Description=Refresh the GitHub App installation token every 30 minutes
[Timer]
OnBootSec=10s
OnUnitActiveSec=30min
[Install]
WantedBy=timers.target
""")
open(f"{out}/app_dir", "w").write(app_dir)
PY
  scp -q -i "$KEY" "$ROOT/deploy/app_token.py" "$TMP/github-app-token.service" "$TMP/github-app-token.timer" \
    "$TMP/app_dir" "$HOST:/tmp/"
  "${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
APP_DIR="$(cat /tmp/app_dir)"
install -d -m700 "$APP_DIR"
install -m700 /tmp/app_token.py "$APP_DIR/app_token.py"
install -m644 /tmp/github-app-token.service /tmp/github-app-token.timer /etc/systemd/system/
systemctl daemon-reload
if [[ -f "$APP_DIR/app.json" && -f "$APP_DIR/app.pem" ]]; then
  systemctl enable --now github-app-token.timer >/dev/null
  if systemctl start github-app-token.service; then
    echo "app token timer enabled, token refreshed"
  else
    echo "WARNING: token refresh failed (it retries every 60s). See: journalctl -u github-app-token.service" >&2
  fi
else
  echo "app token timer installed but not enabled: run deploy/app_setup.py first"
fi
REMOTE
fi

if [[ "${1:-}" == "--routes" ]]; then
  "${SSH[@]}" "cd /root/.hermes && python3 /tmp/routes.py /tmp/$(basename "$CONFIG") && hermes config check >/dev/null && echo 'config check OK'"
  echo "Routes written. Restart the Hermes gateway from a separate shell to load them (see README, 'Operating it')."
fi
