#!/usr/bin/env python3
"""Create the bot's GitHub App with one click (GitHub's manifest flow), then install it.

Usage (on the Hermes host). Safest: keep the server on localhost and reach it through an ssh tunnel, so the
one-time code that GitHub puts in the redirect never crosses the network in cleartext:

    scp deploy/app_setup.py root@your-host:/tmp/
    ssh -L 8645:localhost:8645 root@your-host \\
        python3 /tmp/app_setup.py --name my-bot --public-url http://localhost:8645 --app-dir /root/.config/gh-app

Then open the printed URL on the machine running ssh, click "Create GitHub App", and pick the repos to install
it on. The app's private key goes straight from GitHub to --app-dir/app.pem on the host. The server exits once
the app is installed, or after --deadline seconds.

No tunnel possible (e.g. setting up from a phone)? Use --bind 0.0.0.0 --public-url http://your-host:8645, but
know that anyone who sees the redirect URL before you complete it can exchange its code for the private key.

Permissions: issues and pull requests read/write (comments, reactions, reviews), contents and metadata read.
No app webhook: the bridge keeps using per-repo webhooks (deploy/github_hooks.sh).
"""
import argparse
import html
import http.server
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import urllib.parse
import urllib.request

PERMISSIONS = {"issues": "write", "pull_requests": "write", "contents": "read", "metadata": "read"}
REQUEST_TIMEOUT_S = 30


def manifest(name: str, public_url: str, homepage: str) -> dict:
    return {
        "name": name,
        "url": homepage,
        "redirect_url": f"{public_url}/created",
        "setup_url": f"{public_url}/installed",
        "setup_on_update": True,
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": [],
        "hook_attributes": {"url": f"{public_url}/unused", "active": False},
    }


def convert(code: str) -> dict:
    """Exchange the one-time manifest code for the app's id, slug, and private key. No auth needed."""
    req = urllib.request.Request(f"https://api.github.com/app-manifests/{urllib.parse.quote(code)}/conversions",
                                 method="POST", data=b"", headers={
                                     "Accept": "application/vnd.github+json", "User-Agent": "app-setup",
                                     "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def write_private(path: Path, text: str):
    """Atomic write through a unique mode-600 temp file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save(app_dir: Path, conv: dict, installation_id=None):
    app_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(app_dir, 0o700)
    if conv.get("pem"):
        write_private(app_dir / "app.pem", conv["pem"])
    meta_file = app_dir / "app.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    for k_out, k_in in (("app_id", "id"), ("slug", "slug"), ("client_id", "client_id"), ("html_url", "html_url")):
        if conv.get(k_in) is not None:
            meta[k_out] = conv[k_in]
    if installation_id:
        meta["installation_id"] = int(installation_id)
    write_private(meta_file, json.dumps(meta, indent=2))
    return meta


def make_handler(args, state):
    target = (f"https://github.com/organizations/{args.org}/settings/apps/new" if args.org
              else "https://github.com/settings/apps/new")

    class H(http.server.BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S   # a slow client can't stall the single-threaded server

        def _send(self, code, body, location=None):
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, fmt, *a):
            # request lines carry the one-time code and the nonce: log the path only
            print(f"app_setup: {self.command} {urllib.parse.urlparse(self.path).path}", file=sys.stderr)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            ok_state = secrets.compare_digest(q.get("state", ""), state["nonce"])
            if u.path == f"/{state['nonce']}":
                m = html.escape(json.dumps(manifest(args.name, args.public_url, args.homepage)))
                return self._send(200, f"""<!doctype html><meta name=viewport content="width=device-width">
<form id=f method=post action="{target}?state={state['nonce']}">
<input type=hidden name=manifest value="{m}"><button>Create GitHub App on GitHub</button></form>
<script>document.getElementById('f').submit()</script>""")
            if u.path == "/created" and ok_state and q.get("code") and not state.get("slug"):
                try:
                    conv = convert(q["code"])
                except Exception as e:
                    return self._send(500, f"Conversion failed: {html.escape(str(e))}")
                meta = save(Path(args.app_dir), conv)
                state["slug"] = meta["slug"]
                print(f"app_setup: created app {meta['slug']} (id {meta['app_id']})", file=sys.stderr)
                # GitHub passes state through to setup_url, so /installed can check it too
                return self._send(302, "", f"https://github.com/apps/{meta['slug']}/installations/new"
                                           f"?state={state['nonce']}")
            if (u.path == "/installed" and ok_state and state.get("slug")
                    and q.get("installation_id", "").isdigit()):
                save(Path(args.app_dir), {}, q["installation_id"])
                print(f"app_setup: installed, installation {q['installation_id']}", file=sys.stderr)
                state["done"] = True
                return self._send(200, "Installed. You can close this tab.")
            return self._send(404, "Not found")

        def do_POST(self):
            return self._send(404, "Not found")

    return H


def serve(srv, state, deadline_s: float) -> bool:
    """Handle requests until setup completes or the deadline passes. Returns True when done."""
    end = time.monotonic() + deadline_s
    while not state.get("done"):
        left = end - time.monotonic()
        if left <= 0:
            return False
        srv.timeout = min(left, REQUEST_TIMEOUT_S)
        srv.handle_request()
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", required=True, help="app name; its slug becomes the bot login <slug>[bot]")
    ap.add_argument("--public-url", required=True, help="how your browser reaches this server, no trailing slash")
    ap.add_argument("--app-dir", required=True)
    ap.add_argument("--port", type=int, default=8645)
    ap.add_argument("--bind", default="127.0.0.1", help="listen address (default localhost, use an ssh tunnel)")
    ap.add_argument("--deadline", type=float, default=900, help="give up after this many seconds (default 15 min)")
    ap.add_argument("--org", help="create the app under an organization instead of your account")
    ap.add_argument("--homepage", default="https://github.com/loqimean/hermes-github-bridge")
    args = ap.parse_args(argv)
    args.public_url = args.public_url.rstrip("/")
    state = {"nonce": secrets.token_urlsafe(16)}
    srv = http.server.HTTPServer((args.bind, args.port), make_handler(args, state))
    print(f"Open in a browser signed in to GitHub:\n  {args.public_url}/{state['nonce']}", flush=True)
    try:
        if not serve(srv, state, args.deadline):
            sys.exit(f"setup not finished within {args.deadline:.0f}s; run it again"
                     + (" (the app was created: install it from its settings page, then run app_token.py)"
                        if state.get("slug") else ""))
    finally:
        srv.server_close()
    print((Path(args.app_dir) / "app.json").read_text())


if __name__ == "__main__":
    main()
