"""Free-space diagnostics on disposable workspaces."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from repairlib.__main__ import main
from repairlib.lifecycle.workspace import create_workspace, database_path


class DiskSpaceReportingTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.workspace = Path(root) / "workspace"

    def command(self, *args):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args, "--workspace", str(self.workspace)]), 0)
        return output.getvalue()

    def test_reports_bytes_on_the_upload_filesystem_without_writing(self):
        create_workspace(self.workspace, "Synthetic Shop", "UTC", "owner", "unused")
        path = database_path(self.workspace)
        before = {p: p.read_bytes() if p.is_file() else None for p in self.workspace.rglob("*")}

        def disk_usage(directory):
            self.assertEqual(directory, path.parent)
            return SimpleNamespace(free=0)

        with patch("shutil.disk_usage", side_effect=disk_usage):
            for command in ("doctor", "status"):
                with self.subTest(command=command):
                    self.assertIn("Disk space: 0 bytes free (reserve: 1073741824 bytes)",
                                  self.command(command))
            report = json.loads(self.command("status", "--json"))
            self.assertEqual((report["disk_free_bytes"], report["disk_reserve_bytes"]),
                             (0, 1 << 30))
            self.assertTrue(report["ready"])
        self.assertEqual(
            {p: p.read_bytes() if p.is_file() else None for p in self.workspace.rglob("*")}, before)

    def test_missing_workspace_reports_unavailable_without_creating_it(self):
        self.assert_unavailable()
        self.assertFalse(self.workspace.exists())

    def test_unreadable_disk_probe_reports_unavailable_without_writing(self):
        self.workspace.mkdir()
        with patch("shutil.disk_usage", side_effect=PermissionError) as probe:
            self.assert_unavailable()
        probe.assert_called_with(self.workspace)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def assert_unavailable(self):
        for command in ("doctor", "status"):
            with self.subTest(command=command):
                self.assertIn("Disk space: unavailable (reserve: 1073741824 bytes)",
                              self.command(command))
        report = json.loads(self.command("status", "--json"))
        self.assertEqual((report["disk_free_bytes"], report["disk_reserve_bytes"]),
                         (None, 1 << 30))
