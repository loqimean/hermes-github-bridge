#!/usr/bin/env bash
# Deploy the gate script (and optionally the routes) to the Hermes host, driven by config.json.
#
#   deploy/install.sh            # gate script + gate config only: live immediately, no restart
#   deploy/install.sh --routes   # also rewrite routes in Hermes config.yaml: needs a gateway restart
#
# CONFIG=path/to/config.json overrides the default ./config.json.
# This never restarts the gateway: doing that from inside a Hermes session kills the session.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config.json}"
[[ -f "$CONFIG" ]] || { echo "No $CONFIG. Copy config.example.json to config.json and fill it in." >&2; exit 1; }

read_cfg() { python3 -c "import json,os,sys;c=json.load(open('$CONFIG'));v=eval(sys.argv[1]);print(os.path.expanduser(v) if isinstance(v,str) else v)" "$1"; }
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

# GitHub App mode: install the token minter and a systemd timer that refreshes the token every 30 minutes
APP_DIR="$(read_cfg 'c.get("app", {}).get("dir", "")')"
if [[ -n "$APP_DIR" ]]; then
  TOKEN_FILE="$(read_cfg 'c["gate"]["token_file"]')"
  PUSH="$(read_cfg '" ".join("--push " + t for t in c["app"].get("push", []))')"
  scp -q -i "$KEY" "$ROOT/deploy/app_token.py" "$HOST:/tmp/app_token.py"
  "${SSH[@]}" bash -s -- "$APP_DIR" "$TOKEN_FILE" "$PUSH" <<'REMOTE'
set -euo pipefail
APP_DIR="$1"; TOKEN_FILE="$2"; PUSH="$3"
install -d -m700 "$APP_DIR"
install -m700 /tmp/app_token.py "$APP_DIR/app_token.py"
cat > /etc/systemd/system/github-app-token.service <<UNIT
[Unit]
Description=Refresh the GitHub App installation token for the webhook bridge
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $APP_DIR/app_token.py --app-dir $APP_DIR --out $TOKEN_FILE $PUSH
UNIT
cat > /etc/systemd/system/github-app-token.timer <<UNIT
[Unit]
Description=Refresh the GitHub App installation token every 30 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=30min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
if [[ -f "$APP_DIR/app.json" && -f "$APP_DIR/app.pem" ]]; then
  systemctl enable --now github-app-token.timer >/dev/null
  systemctl start github-app-token.service
  echo "app token timer enabled, token refreshed"
else
  echo "app token timer installed but not enabled: run deploy/app_setup.py first"
fi
REMOTE
fi
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

if [[ "${1:-}" == "--routes" ]]; then
  "${SSH[@]}" "cd /root/.hermes && python3 /tmp/routes.py /tmp/$(basename "$CONFIG") && hermes config check >/dev/null && echo 'config check OK'"
  echo "Routes written. Restart the Hermes gateway from a separate shell to load them (see README, 'Operating it')."
fi
