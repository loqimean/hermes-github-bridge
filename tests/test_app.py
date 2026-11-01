"""Offline tests for GitHub App mode: gate mention handling for "<slug>[bot]" logins, the token minter,
and the manifest setup helper. No network: GitHub calls are faked, openssl signs with a throwaway key.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
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
        self.assertTrue(self.g.addressed_elsewhere("@acme-helper-ai hi"))
        self.assertTrue(self.g.addressed_elsewhere("@bob please"))

    def test_explicit_mention_override(self):
        g = load_gate({"bot_login": "acme-helper[bot]", "mention": "helper", "owner_login": "loqimean"})
        self.assertTrue(g.mentions("@helper ping"))
        self.assertFalse(g.mentions("@acme-helper ping"))

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
        with mock.patch.object(app_token, "api", fake_api), mock.patch.object(app_token, "push") as push:
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
        with mock.patch.object(app_token, "api", fake):
            app_token.main(["--app-dir", str(self.dir), "--out", str(self.dir / "tok")])
        self.assertEqual(paths, ["/app/installations/9/access_tokens"])


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
