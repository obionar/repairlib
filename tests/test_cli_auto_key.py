"""`--key auto` mints a fresh idempotency key instead of requiring the caller to invent one."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]


class AutoKeyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "workspace"
        result = subprocess.run(
            [*COMMAND, "setup", "--workspace", str(self.workspace), "--shop", "Synthetic",
             "--timezone", "UTC", "--username", "owner"],
            input=PASSWORD + "\n", capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def create(self, key, name):
        return subprocess.run(
            [*COMMAND, "customers", "create", "--workspace", str(self.workspace),
             "--key", key, "--name", name, "--json"],
            capture_output=True, text=True, env=python_env(), timeout=30)

    def names(self):
        listing = subprocess.run(
            [*COMMAND, "customers", "list", "--workspace", str(self.workspace), "--json"],
            capture_output=True, text=True, env=python_env(), timeout=30)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        payload = json.loads(listing.stdout)
        items = payload["items"] if isinstance(payload, dict) else payload
        return [item["name"] for item in items]

    def test_auto_key_creates_once_per_call(self):
        first = self.create("auto", "First")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.create("auto", "Second")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(sorted(self.names()), ["First", "Second"])

    def test_explicit_key_still_replays(self):
        self.assertEqual(self.create("fixed-key", "Only once").returncode, 0)
        replay = self.create("fixed-key", "Only once")
        self.assertIn(replay.returncode, (0, 1))
        self.assertEqual(self.names().count("Only once"), 1)


if __name__ == "__main__":
    unittest.main()
