"""Offline tests for GitHub App mode: gate mention handling for "<slug>[bot]" logins, the token minter,
and the manifest setup helper. No network: GitHub calls are faked, openssl signs with a throwaway key.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from test_gate import load_gate  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


app_token = load(ROOT / "deploy" / "app_token.py", "app_token")
app_setup = load(ROOT / "deploy" / "app_setup.py", "app_setup")


class AppMentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load_gate({"bot_login": "acme-helper[bot]", "owner_login": "loqimean"})

    def test_handle_derived_from_bot_login(self):
        self.assertEqual(self.g.HANDLE, "acme-helper")

    def test_mentions(self):
        g = self.g
        self.assertTrue(g.mentions("@acme-helper can you check CI"))
        self.assertTrue(g.mentions("@acme-helper[bot] look"))
        self.assertTrue(g.mentions("thanks @Acme-Helper."))
        self.assertFalse(g.mentions("@acme-helper-ai hi"))
        self.assertFalse(g.mentions("@acme-helperx hi"))
        self.assertFalse(g.mentions("mail me@acme-helper.dev"))

    def test_own_handle_is_not_someone_else(self):
        self.assertFalse(self.g.addressed_elsewhere("@acme-helper hi"))
        self.assertFalse(self.g.addressed_elsewhere("@acme-helper[bot] hi"))
        self.assertFalse(self.g.OTHER_MENTION.search("@acme-helper[bot] and @ACME-HELPER"))
        self.assertTrue(self.g.addressed_elsewhere("@acme-helper-ai hi"))
        self.assertTrue(self.g.addressed_elsewhere("@bob please"))

    def test_explicit_mention_override(self):
        g = load_gate({"bot_login": "acme-helper[bot]", "mention": "helper", "owner_login": "loqimean"})
        self.assertTrue(g.mentions("@helper ping"))
        # the real login GitHub displays still works next to the override
        self.assertTrue(g.mentions("@acme-helper[bot] ping"))
        self.assertTrue(g.mentions("@acme-helper ping"))
        self.assertFalse(g.addressed_elsewhere("@acme-helper[bot] ping"))
        self.assertFalse(g.mentions("@helperx ping"))

    def test_engaged_matches_bot_login(self):
        g = self.g
        with mock.patch.object(g, "gh_get", return_value=[{"user": {"login": "acme-helper[bot]"}}]):
            self.assertTrue(g.engaged("o/r", 1, {}, False))
        with mock.patch.object(g, "gh_get", return_value=[{"user": {"login": "acme-helper"}}]):
            self.assertFalse(g.engaged("o/r", 1, {}, False))


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class AppTokenTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pem = self.dir / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.pem), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(self.pem), "-pubout", "-out", str(self.dir / "pub.pem")],
                       check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_jwt_claims_and_signature(self):
        jwt = app_token.app_jwt(123, str(self.pem), now=1_000_000)
        head, body, sig = jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        import base64
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head))), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
        self.assertEqual(claims, {"iat": 999_940, "exp": 1_000_540, "iss": "123"})
        (self.dir / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
        (self.dir / "msg").write_bytes(f"{head}.{body}".encode())
        v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.dir / "pub.pem"),
                            "-signature", str(self.dir / "sig"), str(self.dir / "msg")], capture_output=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_main_finds_installation_writes_private_token(self):
        (self.dir / "app.json").write_text(json.dumps({"app_id": 5}))
        calls = []

        def fake_api(method, path, jwt):
            calls.append((method, path))
            if path == "/app/installations":
                return [{"id": 77}]
            return {"token": "ghs_test", "expires_at": "2030-01-01T00:00:00Z"}

        out = self.dir / "sub" / "token"
        with mock.patch.object(app_token, "api", fake_api), mock.patch.object(app_token, "push") as push, \
                redirect_stdout(io.StringIO()):
            app_token.main(["--app-dir", str(self.dir), "--out", str(out), "--push", "u@h:/t"])
        self.assertEqual(calls, [("GET", "/app/installations"), ("POST", "/app/installations/77/access_tokens")])
        self.assertEqual(out.read_text(), "ghs_test")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.dir / "app.json").read_text())["installation_id"], 77)
        push.assert_called_once_with("ghs_test", "u@h:/t")

    def test_main_uses_saved_installation(self):
        (self.dir / "app.json").write_text(json.dumps({"app_id": 5, "installation_id": 9}))
        paths = []
        fake = lambda m, p, j: paths.append(p) or {"token": "t", "expires_at": "x"}
        with mock.patch.object(app_token, "api", fake), redirect_stdout(io.StringIO()):
            app_token.main(["--app-dir", str(self.dir), "--out", str(self.dir / "tok")])
        self.assertEqual(paths, ["/app/installations/9/access_tokens"])

    def test_failed_push_keeps_local_token_and_tries_every_target(self):
        (self.dir / "app.json").write_text(json.dumps({"app_id": 5, "installation_id": 9}))
        tried = []

        def flaky(token, target):
            tried.append(target)
            if target.startswith("bad@"):
                raise subprocess.CalledProcessError(255, "ssh")

        fake = lambda m, p, j: {"token": "ghs_x", "expires_at": "x"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(app_token, "api", fake), mock.patch.object(app_token, "push", flaky), \
                redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            app_token.main(["--app-dir", str(self.dir), "--out", str(self.dir / "tok"),
                            "--push", "bad@h1:/t", "--push", "ok@h2:/t"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(tried, ["bad@h1:/t", "ok@h2:/t"])
        self.assertEqual((self.dir / "tok").read_text(), "ghs_x")
        self.assertNotIn("ghs_x", out.getvalue() + err.getvalue())

    def test_leftover_tmp_file_cannot_loosen_permissions(self):
        out = self.dir / "tok"
        loose = self.dir / "tok.tmp"
        loose.write_text("old"); os.chmod(loose, 0o644)
        app_token.write_private(str(out), "new")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.read_text(), "new")


class PushCommandTests(unittest.TestCase):
    def test_argv_quotes_path_and_ends_options(self):
        argv = app_token.push_command("me@box:/home/me/my dir/token")
        self.assertEqual(argv[0], "ssh")
        i = argv.index("--")
        self.assertEqual(argv[i + 1], "me@box")
        self.assertIn("'/home/me/my dir/token'", argv[i + 2])
        self.assertIn("BatchMode=yes", argv)

    def test_rejects_unsafe_targets(self):
        for bad in ["-oProxyCommand=x:/t", "me@box:~/token", "me@box:relative", "no-colon", ":/t"]:
            with self.assertRaises(ValueError, msg=bad):
                app_token.push_command(bad)

    def test_token_goes_on_stdin_only(self):
        with mock.patch.object(app_token.subprocess, "run") as run:
            app_token.push("ghs_secret", "me@box:/t")
        args, kwargs = run.call_args
        self.assertNotIn("ghs_secret", " ".join(args[0]))
        self.assertEqual(kwargs["input"], b"ghs_secret")
        self.assertEqual(kwargs["timeout"], app_token.PUSH_TIMEOUT_S)


class SetupHandlerTests(unittest.TestCase):
    """Drives the setup handler with fake requests: state checks, ordering, no code in logs."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "app"
        args = mock.Mock(name="args", org=None, homepage="https://example.com", public_url="http://localhost:8645",
                         app_dir=str(self.dir))
        args.name = "acme-helper"
        self.state = {"nonce": "N0NCE"}
        self.H = app_setup.make_handler(args, self.state)

    def tearDown(self):
        shutil.rmtree(self.dir.parent)

    def get(self, path):
        h = self.H.__new__(self.H)
        h.path, h.command, h.request_version = path, "GET", "HTTP/1.1"
        h.wfile, h.requestline, h.client_address = io.BytesIO(), f"GET {path} HTTP/1.1", ("127.0.0.1", 1)
        h.close_connection = True
        sent = {}
        h.send_response = lambda code, msg=None: sent.setdefault("code", code)
        h.send_header = lambda k, v: sent.setdefault(k, v)
        h.end_headers = lambda: None
        err = io.StringIO()
        with redirect_stderr(err):
            h.do_GET()
        sent["log"] = err.getvalue()
        return sent

    def test_form_page_needs_nonce(self):
        self.assertEqual(self.get("/N0NCE")["code"], 200)
        self.assertEqual(self.get("/wrong")["code"], 404)

    def test_created_requires_state(self):
        with mock.patch.object(app_setup, "convert") as conv:
            self.assertEqual(self.get("/created?code=c&state=bad")["code"], 404)
            conv.assert_not_called()

    def test_full_flow(self):
        conv = {"id": 1, "slug": "acme-helper", "pem": "KEY"}
        with mock.patch.object(app_setup, "convert", return_value=conv):
            r = self.get("/created?code=SECRETCODE&state=N0NCE")
        self.assertEqual(r["code"], 302)
        self.assertEqual(r["Location"], "https://github.com/apps/acme-helper/installations/new?state=N0NCE")
        self.assertNotIn("SECRETCODE", r["log"])
        # a second /created can't overwrite the app
        with mock.patch.object(app_setup, "convert") as again:
            self.assertEqual(self.get("/created?code=other&state=N0NCE")["code"], 404)
            again.assert_not_called()
        self.assertEqual(self.get("/installed?installation_id=5&state=bad")["code"], 404)
        self.assertEqual(self.get("/installed?installation_id=abc&state=N0NCE")["code"], 404)
        self.assertFalse(self.state.get("done"))
        self.assertEqual(self.get("/installed?installation_id=42&state=N0NCE")["code"], 200)
        self.assertTrue(self.state["done"])
        self.assertEqual(json.loads((self.dir / "app.json").read_text())["installation_id"], 42)

    def test_installed_before_created_is_rejected(self):
        self.assertEqual(self.get("/installed?installation_id=42&state=N0NCE")["code"], 404)
        self.assertFalse(self.state.get("done"))

    def test_serve_gives_up_at_deadline(self):
        srv = mock.Mock()
        self.assertFalse(app_setup.serve(srv, {}, deadline_s=0))
        srv.handle_request.assert_not_called()


class AppSetupTests(unittest.TestCase):
    def test_manifest(self):
        m = app_setup.manifest("acme-helper", "http://h:8645", "https://example.com")
        self.assertEqual(m["redirect_url"], "http://h:8645/created")
        self.assertEqual(m["setup_url"], "http://h:8645/installed")
        self.assertFalse(m["public"])
        self.assertFalse(m["hook_attributes"]["active"])
        self.assertEqual(m["default_permissions"],
                         {"issues": "write", "pull_requests": "write", "contents": "read", "metadata": "read"})

    def test_save_keeps_key_private_and_merges_installation(self):
        d = Path(tempfile.mkdtemp()) / "app"
        try:
            app_setup.save(d, {"id": 1, "slug": "acme-helper", "pem": "KEY", "client_id": "c", "webhook_secret": "s"})
            self.assertEqual((d / "app.pem").read_text(), "KEY")
            self.assertEqual((d / "app.pem").stat().st_mode & 0o777, 0o600)
            meta = app_setup.save(d, {}, "42")
            self.assertEqual(meta, {"app_id": 1, "slug": "acme-helper", "client_id": "c", "installation_id": 42})
            self.assertNotIn("KEY", (d / "app.json").read_text())
        finally:
            shutil.rmtree(d.parent)


if __name__ == "__main__":
    unittest.main()
