"""Private installer selection must be durable before admitting writes."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from repairlib.lifecycle import update


class UpdateDurabilityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.descriptor = Path(directory) / "current.json"
        self.ready = {"format": 1, "stage": {"candidate_workspace": "/synthetic/workspace"}, "state": "ready"}
        self.open = self.ready | {"state": "open"}

    def publish(self, selection):
        update.publish_bytes(self.descriptor, json.dumps(selection).encode())

    def test_file_is_synced_before_rename_and_directory_after(self):
        self.publish(self.ready)
        events = []
        fsync = os.fsync

        def record(fd):
            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append((kind, update.read_selection(self.descriptor)["state"]))
            fsync(fd)

        with patch("os.fsync", side_effect=record):
            self.publish(self.open)
        self.assertEqual(events, [("file", "ready"), ("directory", "open")])

    def test_reader_will_not_admit_writes_after_directory_sync_failure(self):
        self.publish(self.ready)
        read = update.selection_reader(self.descriptor, self.ready)
        self.assertEqual(read(), {"update_phase": "ready", "writes_open": False})
        with patch.object(update, "sync_directory", side_effect=OSError("failed to sync")):
            with self.assertRaises(OSError):
                self.publish(self.open)
            # Rename is visible even though publication failed; the listener
            # must not accept this open state unless it can sync it itself.
            self.assertEqual(update.read_selection(self.descriptor), self.open)
            self.assertEqual(read(), {"update_phase": "unavailable", "writes_open": False})
        with patch.object(update, "sync_directory", wraps=update.sync_directory) as sync:
            self.assertEqual(read(), {"update_phase": "open", "writes_open": True})
            self.assertTrue(read()["writes_open"])
            sync.assert_called_once_with(self.descriptor.parent)
