#!/usr/bin/env python3
"""Mint a GitHub App installation token and write it where the gate and the agent read it.

Usage (on the Hermes host, run by a timer every 30 minutes; tokens live 60):
    python3 app_token.py --app-dir /root/.config/gh-app --out /root/.config/gh-bot/token \\
        [--push user@agent-host:/absolute/path/to/token]

--app-dir holds app.json ({"app_id": ..., "installation_id": ...}) and app.pem, both written by
app_setup.py. If installation_id is missing, the first installation of the app is used and saved.
--push copies the token over ssh to the terminal backend the agent runs on. The path must be absolute,
and root on this host needs an ssh key authorized there plus a known_hosts entry (BatchMode is on).

The local token is always written first. Every push target is tried; the exit code is 1 if any failed.
Stdlib only: the JWT is signed with the openssl CLI. The token only ever travels on stdin, never in argv.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request

API = "https://api.github.com"
PUSH_TIMEOUT_S = 60


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id, pem_path: str, now: int = None) -> str:
    """RS256 JWT for the app, valid 9 minutes (GitHub caps it at 10), backdated 60s for clock skew."""
    now = int(time.time()) if now is None else now
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", pem_path], input=signing_input,
                         capture_output=True, check=True, timeout=30).stdout
    return f"{head}.{body}.{b64url(sig)}"


def api(method: str, path: str, jwt: str):
    req = urllib.request.Request(f"{API}{path}", method=method, data=b"" if method == "POST" else None, headers={
        "Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "app-token"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def write_private(path: str, text: str):
    """Atomic write through a unique mode-600 temp file, so readers never see a partial or loose-mode file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def push_command(target: str):
    """ssh argv for one target. Raises ValueError on targets we can't push to safely."""
    host, sep, path = target.partition(":")
    if not sep or not host or host.startswith("-"):
        raise ValueError(f"bad push target {target!r}: expected user@host:/absolute/path")
    if not path.startswith("/"):
        raise ValueError(f"bad push target {target!r}: path must be absolute (no ~)")
    d, q = shlex.quote(os.path.dirname(path)), shlex.quote(path)
    tmp = shlex.quote(path + ".tmp")
    remote = f"umask 077 && mkdir -p {d} && cat > {tmp} && mv -f {tmp} {q}"
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3", "--", host, remote]


def push(token: str, target: str):
    subprocess.run(push_command(target), input=token.encode(), check=True, timeout=PUSH_TIMEOUT_S,
                   stdout=subprocess.DEVNULL)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--push", action="append", default=[], help="user@host:/absolute/path (repeatable)")
    a = ap.parse_args(argv)

    app_file = Path(a.app_dir) / "app.json"
    app = json.loads(app_file.read_text())
    jwt = app_jwt(app["app_id"], str(Path(a.app_dir) / "app.pem"))
    inst = app.get("installation_id")
    if not inst:
        installs = api("GET", "/app/installations", jwt)
        if not installs:
            sys.exit("app has no installations yet: install it on your repos first")
        inst = installs[0]["id"]
        app["installation_id"] = inst
        write_private(str(app_file), json.dumps(app, indent=2))
    tok = api("POST", f"/app/installations/{inst}/access_tokens", jwt)
    write_private(a.out, tok["token"])
    print(f"token written, expires {tok['expires_at']}")
    failed = 0
    for target in a.push:
        host = target.partition(":")[0]
        try:
            push(tok["token"], target)
            print(f"pushed to {host}")
        except Exception as e:  # keep going: one bad host must not leave the others with a stale token
            failed += 1
            print(f"push to {host} failed: {type(e).__name__}: {e}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
