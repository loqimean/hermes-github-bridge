"""Offline tests for deploy/routes.py against a throwaway Hermes config."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
EXAMPLE = json.loads((ROOT / "config.example.json").read_text())
spec = importlib.util.spec_from_file_location("routes", ROOT / "deploy" / "routes.py")
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)


class BuildRouteTests(unittest.TestCase):
    def test_owner_filter_and_identity(self):
        r = routes.build_route(EXAMPLE, {"profile": None, "opus_subagent": False}, "s3cret")
        self.assertEqual(r["filters"], [{"field": "sender.login", "equals": "your-github-login"}])
        self.assertIn("as user your-app-slug[bot]", r["prompt"])
        self.assertIn(EXAMPLE["agent_token_path"], r["prompt"])
        self.assertNotIn("delegation", r["toolsets"])
        self.assertNotIn("profile", r)
        self.assertEqual(r["deliver"], "log")

    def test_opus_profile_route(self):
        r = routes.build_route(EXAMPLE, {"profile": "api", "opus_subagent": True}, "s3cret")
        self.assertEqual(r["profile"], "api")
        self.assertIn("delegation", r["toolsets"])
        self.assertIn("delegate_task", r["prompt"])

    def test_template_fields_survive(self):
        r = routes.build_route(EXAMPLE, {"profile": None}, "s")
        for field in ("{bot.kind}", "{bot.thread}", "{bot.instant_reply}", "{bot.react_path}", "{bot.tier}"):
            self.assertIn(field, r["prompt"])


class MainTests(unittest.TestCase):
    def run_routes(self, existing_routes):
        d = tempfile.mkdtemp()
        hermes = Path(d, "config.yaml")
        hermes.write_text(yaml.safe_dump({"model": {"default": "x"},
                                          "platforms": {"webhook": {"extra": {"routes": existing_routes}}}}))
        cfg = dict(EXAMPLE, legacy_routes=["old-route"])
        cfg_path = Path(d, "config.json"); cfg_path.write_text(json.dumps(cfg))
        out = subprocess.run([sys.executable, str(ROOT / "deploy" / "routes.py"), str(cfg_path)],
                             env={**os.environ, "HERMES_CONFIG": str(hermes)}, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return yaml.safe_load(hermes.read_text())

    def test_idempotent_keeps_secrets_and_removes_legacy(self):
        c = self.run_routes({"old-route": {"secret": "x"}, "github": {"secret": "keep-me"}, "other": {"secret": "y"}})
        r = c["platforms"]["webhook"]["extra"]["routes"]
        self.assertNotIn("old-route", r)
        self.assertIn("other", r)                       # unrelated routes untouched
        self.assertEqual(r["github"]["secret"], "keep-me")
        self.assertTrue(r["github-api"]["secret"])      # new route gets a fresh secret
        self.assertEqual(c["model"], {"default": "x"})  # rest of config untouched


if __name__ == "__main__":
    unittest.main()
