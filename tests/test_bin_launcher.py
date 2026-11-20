"""bin/repairlib fills the workspace and port defaults from the checkout."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "repairlib"


class BinLauncherTests(unittest.TestCase):
    def test_help_runs_from_the_checkout(self):
        done = subprocess.run([sys.executable, str(BIN), "--help"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_status_defaults_to_the_checkout_data_directory(self):
        done = subprocess.run([sys.executable, str(BIN), "status"], capture_output=True, text=True)
        self.assertIn(str(ROOT / "data"), done.stdout)


if __name__ == "__main__":
    unittest.main()
