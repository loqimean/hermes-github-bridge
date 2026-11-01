#!/usr/bin/env python3
"""Create the bot's GitHub App with one click (GitHub's manifest flow), then install it.

Usage (on the Hermes host, with a port GitHub's redirect can reach from your browser):
    python3 app_setup.py --name my-bot --public-url http://your-host:8645 --app-dir /root/.config/gh-app \\
        [--port 8645] [--org your-org]

Open the printed URL in a browser where you're signed in to GitHub, click "Create GitHub App", then pick the
repos to install it on. The app's private key goes straight from GitHub to --app-dir/app.pem on this host:
it never passes through your browser or chat. The server exits once the app is installed.

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
import urllib.parse
import urllib.request

PERMISSIONS = {"issues": "write", "pull_requests": "write", "contents": "read", "metadata": "read"}


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


def save(app_dir: Path, conv: dict, installation_id=None):
    app_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(app_dir, 0o700)
    if conv.get("pem"):
        pem = app_dir / "app.pem"
        fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(conv["pem"])
    meta_file = app_dir / "app.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    for k_out, k_in in (("app_id", "id"), ("slug", "slug"), ("client_id", "client_id"), ("html_url", "html_url")):
        if conv.get(k_in) is not None:
            meta[k_out] = conv[k_in]
    if installation_id:
        meta["installation_id"] = int(installation_id)
    fd = os.open(meta_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def make_handler(args, state):
    target = (f"https://github.com/organizations/{args.org}/settings/apps/new" if args.org
              else "https://github.com/settings/apps/new")

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, location=None):
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, fmt, *a):
            print("app_setup:", fmt % a, file=sys.stderr)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path == f"/{state['nonce']}":
                m = html.escape(json.dumps(manifest(args.name, args.public_url, args.homepage)))
                return self._send(200, f"""<!doctype html><meta name=viewport content="width=device-width">
<form id=f method=post action="{target}?state={state['nonce']}">
<input type=hidden name=manifest value="{m}"><button>Create GitHub App on GitHub</button></form>
<script>document.getElementById('f').submit()</script>""")
            if u.path == "/created" and q.get("state") == state["nonce"] and q.get("code"):
                try:
                    conv = convert(q["code"])
                except Exception as e:
                    return self._send(500, f"Conversion failed: {html.escape(str(e))}")
                meta = save(Path(args.app_dir), conv)
                state["slug"] = meta["slug"]
                print(f"app_setup: created app {meta['slug']} (id {meta['app_id']})", file=sys.stderr)
                return self._send(302, "", f"https://github.com/apps/{meta['slug']}/installations/new")
            if u.path == "/installed" and q.get("installation_id") and state.get("slug"):
                save(Path(args.app_dir), {}, q["installation_id"])
                print(f"app_setup: installed, installation {q['installation_id']}", file=sys.stderr)
                state["done"] = True
                return self._send(200, "Installed. You can close this tab.")
            return self._send(404, "Not found")

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", required=True, help="app name; its slug becomes the bot login <slug>[bot]")
    ap.add_argument("--public-url", required=True, help="how your browser reaches this server, no trailing slash")
    ap.add_argument("--app-dir", required=True)
    ap.add_argument("--port", type=int, default=8645)
    ap.add_argument("--org", help="create the app under an organization instead of your account")
    ap.add_argument("--homepage", default="https://github.com/loqimean/hermes-github-bridge")
    args = ap.parse_args(argv)
    args.public_url = args.public_url.rstrip("/")
    state = {"nonce": secrets.token_urlsafe(16)}
    srv = http.server.HTTPServer(("0.0.0.0", args.port), make_handler(args, state))
    print(f"Open in a browser signed in to GitHub:\n  {args.public_url}/{state['nonce']}", flush=True)
    while not state.get("done"):
        srv.handle_request()
    print(json.dumps({k: v for k, v in json.loads((Path(args.app_dir) / "app.json").read_text()).items()}))


if __name__ == "__main__":
    main()
