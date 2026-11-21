"""CLI file-path mistakes use the documented exit codes, not the internal-error code."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]


class CliInputErrorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        initialize_workspace(self.workspace, shop="Synthetic", timezone="UTC",
                             username="owner", password=PASSWORD)
        self.repair = json.loads(self.cli(
            "repairs", "create", "--key", "repair-key", "--device-type", "Laptop",
            "--reported-issue", "Synthetic issue", "--json"))["id"]

    def cli(self, *args, code=0):
        result = subprocess.run([*COMMAND, *map(str, args), "--workspace", str(self.workspace)],
                                capture_output=True, text=True, env=python_env(), timeout=30)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout

    def attachment(self):
        source = self.root / "photo.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n synthetic bytes")
        return json.loads(self.cli("repairs", "attach", self.repair, source,
                                   "--key", "attach-key", "--json"))["id"]

    def test_download_refuses_an_existing_output_as_a_conflict(self):
        attachment = self.attachment()
        output = self.root / "copy.png"
        self.cli("repairs", "download", self.repair, attachment, "--output", output)
        result = subprocess.run(
            [*COMMAND, "repairs", "download", str(self.repair), str(attachment),
             "--output", str(output), "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("already exists", result.stderr)

    def test_attach_reports_a_missing_or_non_file_source_as_invalid_input(self):
        self.cli("repairs", "attach", self.repair, self.root / "absent.bin",
                 "--key", "missing-key", code=8)
        self.cli("repairs", "attach", self.repair, self.root, "--key", "dir-key", code=8)

    def test_csv_reports_an_unreadable_path_as_invalid_input(self):
        self.cli("csv", "customers", "preview", self.root / "absent.csv", code=8)
        self.cli("csv", "customers", "preview", self.root, code=8)

    def test_portable_export_refuses_an_existing_path_as_a_conflict(self):
        output = self.root / "shop.zip"
        self.cli("portable", "export", output, "--json")
        self.cli("portable", "export", output, "--json", code=7)

    def test_portable_preview_reports_an_unreadable_path_as_invalid_input(self):
        self.cli("portable", "preview", self.root / "absent.zip", code=8)
        self.cli("portable", "preview", self.root, code=8)


    def test_fees_reports_a_malformed_amount_as_invalid_input(self):
        result = subprocess.run(
            [*COMMAND, "repairs", "fees", str(self.repair), "--revision", "1",
             "--key", "fee-key", "--discount", "abc", "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=30)
        self.assertEqual(result.returncode, 8, (result.stdout, result.stderr))
        self.assertIn("plain non-negative number", result.stderr)
        self.assertNotIn("workspace", result.stderr.lower())

if __name__ == "__main__":
    unittest.main()
