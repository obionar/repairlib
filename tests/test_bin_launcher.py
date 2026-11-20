"""bin/repairlib fills the workspace and port defaults from the checkout."""

# ruff: noqa: E402
import subprocess
import sys
from os import environ
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "repairlib"


class BinLauncherTests(unittest.TestCase):
    def isolated(self, *arguments):
        # A developer shell that exports REPAIRLIB_WORKSPACE must not change these tests.
        return subprocess.run([sys.executable, str(BIN), *arguments], capture_output=True, text=True,
                              env={**environ, "REPAIRLIB_WORKSPACE": ""})

    def test_help_runs_from_the_checkout(self):
        done = self.isolated("--help")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_status_defaults_to_the_checkout_data_directory(self):
        done = self.isolated("status")
        self.assertIn(str(ROOT / "data"), done.stdout)

    def test_a_nested_command_takes_the_default_workspace(self):
        done = self.isolated("repairs", "list")
        self.assertNotIn("invalid choice", done.stderr)

    def test_an_explicit_environment_workspace_wins(self):
        done = subprocess.run([sys.executable, str(BIN), "status"], capture_output=True, text=True,
                              env={**environ, "REPAIRLIB_WORKSPACE": "/tmp/other-workspace"})
        self.assertIn("/tmp/other-workspace", done.stdout)


if __name__ == "__main__":
    unittest.main()
