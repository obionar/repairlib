"""Daily online snapshot, retention and stale-backup warnings."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest

from repairlib.lifecycle.backup_schedule import (
    BACKUP_RETENTION_SECONDS, BACKUP_STALE_SECONDS, backup_directory, scheduled_snapshot,
)
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_tokens import TokenFixture


class BackupScheduleTests(TokenFixture, unittest.TestCase):
    def command(self, *args):
        return subprocess.run([*COMMAND, *args], env=python_env(), capture_output=True, text=True, timeout=120)

    def status(self):
        result = self.command("status", "--workspace", str(self.workspace), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_due_snapshot_is_verified_and_not_taken_twice(self):
        snapshot = scheduled_snapshot(self.workspace)
        assert snapshot is not None
        self.assertEqual(snapshot.parent, backup_directory(self.workspace))
        self.assertTrue(snapshot.is_file())
        verified = self.command("backup", "verify", str(snapshot))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        with closing(sqlite3.connect(snapshot)) as copy:
            self.assertEqual(copy.execute("PRAGMA integrity_check(1)").fetchone(), ("ok",))
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                             self.snapshot("SELECT COUNT(*) FROM users")[0][0])
        self.assertIsNone(scheduled_snapshot(self.workspace))

    def test_retention_drops_old_snapshots_and_keeps_the_newest(self):
        directory = backup_directory(self.workspace)
        directory.mkdir(mode=0o700)
        old = directory / "repairlib-20200101-000000.sqlite3"
        old.write_bytes(b"older than the retention window")
        stale = time.time() - BACKUP_RETENTION_SECONDS - 60
        os.utime(old, (stale, stale))
        snapshot = scheduled_snapshot(self.workspace)
        assert snapshot is not None
        self.assertFalse(old.exists())
        self.assertTrue(snapshot.is_file())

    def test_backup_staleness_stays_in_diagnostics_not_dashboard(self):
        report = self.status()
        self.assertEqual((report["backup"]["last_verified_at"], report["backup"]["stale"]), (None, True))
        self.assertIn("Last verified backup: none", self.command(
            "doctor", "--workspace", str(self.workspace)).stdout)
        scheduled_snapshot(self.workspace)
        report = self.status()
        self.assertFalse(report["backup"]["stale"])
        self.assertIsNotNone(report["backup"]["last_verified_at"])
        self.assertIn("Last verified backup:", self.command(
            "doctor", "--workspace", str(self.workspace)).stdout)
        with self.client() as client:
            self.sign_in(client)
            self.assertNotIn("No recent verified backup", client.get("/").text)
            for path in backup_directory(self.workspace).iterdir():
                moment = time.time() - BACKUP_STALE_SECONDS - 60
                os.utime(path, (moment, moment))
            self.assertTrue(self.status()["backup"]["stale"])
            self.assertIn("— stale", client.get("/system").text)
            self.assertNotIn("No recent verified backup", client.get("/").text)


if __name__ == "__main__":
    unittest.main()
