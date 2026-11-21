"""Corrupt scheduled snapshots must not count as usable backups."""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest

from unittest.mock import patch

from repairlib.lifecycle.backup_schedule import backup_directory, backup_state, scheduled_snapshot
from repairlib.lifecycle.recovery import verify_snapshot
from test_tokens import TokenFixture


class BackupFreshnessRegression(TokenFixture, unittest.TestCase):
    def test_corrupt_future_snapshot_does_not_trigger_repeated_backups(self):
        directory = backup_directory(self.workspace)
        directory.mkdir(mode=0o700)
        bad = directory / "repairlib-20200101-000000.sqlite3"
        bad.write_bytes(b"corrupted")
        bad.chmod(0o600)
        now = int(time.time())
        os.utime(bad, (now + 86400, now + 86400))
        created = scheduled_snapshot(self.workspace, now=now)
        self.assertIsNotNone(created)
        if created is None:
            self.fail("Replacement snapshot was not created")
        os.utime(created, (now, now))
        self.assertIsNone(scheduled_snapshot(self.workspace, now=now))
        self.assertTrue(bad.exists())

    def test_removed_retention_target_does_not_report_failure(self):
        from pathlib import Path
        from repairlib.lifecycle.backup_schedule import retain, BACKUP_RETENTION_SECONDS
        directory = backup_directory(self.workspace)
        directory.mkdir(mode=0o700)
        old = directory / "repairlib-20200101-000000.sqlite3"
        old.write_bytes(b"old")
        now = int(time.time())
        os.utime(old, (now - BACKUP_RETENTION_SECONDS - 1,) * 2)
        current = directory / "repairlib-20200102-000000.sqlite3"
        current.write_bytes(b"current")
        unlink = Path.unlink
        def deleted_by_other(path, *args, **kwargs):
            unlink(path)
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", deleted_by_other):
            retain(directory, now)
        self.assertTrue(current.exists())

    def test_corrupt_backup_same_second_is_preserved_and_replaced(self):
        moment = int(time.time())
        original = scheduled_snapshot(self.workspace, now=moment)
        self.assertIsNotNone(original)
        if original is None:
            self.fail("Initial snapshot was not created")
        original.write_bytes(b"corrupted")
        replacement = scheduled_snapshot(self.workspace, now=moment)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement, original)
        self.assertTrue(original.exists())
        if replacement is None:
            self.fail("Replacement snapshot was not created")
        self.assertIsInstance(verify_snapshot(replacement), int)

    def test_two_concurrent_schedulers_do_not_overwrite_each_other(self):
        moment = int(time.time())
        barrier = threading.Barrier(2)
        def both_due(workspace, *, now):
            state = backup_state(workspace, now=now)
            barrier.wait(timeout=10)
            return state
        with patch("repairlib.lifecycle.backup_schedule.backup_state", side_effect=both_due):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(scheduled_snapshot, self.workspace, now=moment) for _ in range(2)]
                results = [future.result(timeout=30) for future in futures]
        self.assertNotEqual(results[0], results[1])
        for path in results:
            self.assertIsNotNone(path)
            if path is not None:
                self.assertIsInstance(verify_snapshot(path), int)

    def test_backup_removed_after_verification_is_stale(self):
        original = scheduled_snapshot(self.workspace)
        self.assertIsNotNone(original)
        if original is None:
            self.fail("Initial snapshot was not created")
        def remove_after_verify(path):
            verify_snapshot(path)
            path.unlink()
        with patch("repairlib.lifecycle.backup_schedule.verify_snapshot", side_effect=remove_after_verify):
            state = backup_state(self.workspace)
        self.assertTrue(state["stale"])
        self.assertIsNone(state["last_verified_at"])

    def test_replaced_backup_after_verification_is_stale(self):
        original = scheduled_snapshot(self.workspace)
        if original is None:
            self.fail("Initial snapshot was not created")
        def replace_after_verify(path):
            verify_snapshot(path)
            path.unlink()
            path.write_bytes(b"different")
        with patch("repairlib.lifecycle.backup_schedule.verify_snapshot", side_effect=replace_after_verify):
            state = backup_state(self.workspace)
        self.assertTrue(state["stale"])
        self.assertIsNone(state["last_verified_at"])

    def test_corrupt_recent_snapshot_is_not_fresh_and_does_not_block_backup(self):
        directory = backup_directory(self.workspace)
        directory.mkdir(mode=0o700)
        bad = directory / "repairlib-20200101-000000.sqlite3"
        bad.write_bytes(b"not a database")
        bad.chmod(0o600)
        state = backup_state(self.workspace)
        self.assertTrue(state["stale"])
        self.assertIsNone(state["last_verified_at"])
        replacement = scheduled_snapshot(self.workspace)
        self.assertIsNotNone(replacement)
        self.assertIsInstance(verify_snapshot(replacement), int)
        self.assertTrue(bad.exists())
        self.assertFalse(backup_state(self.workspace)["stale"])

    def test_corruption_after_verified_snapshot_triggers_new_snapshot(self):
        moment = int(time.time())
        original = scheduled_snapshot(self.workspace, now=moment)
        self.assertIsNotNone(original)
        if original is None:
            self.fail("Initial snapshot was not created")
        original.write_bytes(b"corrupted after creation")
        self.assertTrue(backup_state(self.workspace, now=moment + 1)["stale"])
        replacement = scheduled_snapshot(self.workspace, now=moment + 1)
        self.assertIsNotNone(replacement)
        self.assertTrue(original.exists())
        if replacement is None:
            self.fail("Replacement snapshot was not created")
        self.assertIsInstance(verify_snapshot(replacement), int)
