"""A successful checkout update keeps only the newest recovery archives."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from repairlib.lifecycle.checkout_update import KEEP_RECOVERY, prune_recovery


class RecoveryPruningTests(unittest.TestCase):
    def test_keeps_newest_and_current_and_ignores_everything_else(self):
        with TemporaryDirectory() as root:
            parent = Path(root)
            dirs = []
            for age in range(5):  # 0 is newest
                path = parent / f".repairlib-recovery-{age}"
                (path / "rehearsal").mkdir(parents=True)
                (path / "backup.zip").write_bytes(b"zip")
                os.utime(path, (1_000_000 - age * 100, 1_000_000 - age * 100))
                dirs.append(path)
            current = dirs[4]  # oldest mtime, but it is this run's archive
            outside = parent / "elsewhere"
            outside.mkdir()
            (parent / ".repairlib-recovery-link").symlink_to(outside)
            (parent / ".repairlib-recovery-file").write_text("not a directory")
            (parent / "repairlib").mkdir()
            prune_recovery(parent, current)
            self.assertEqual(KEEP_RECOVERY, 2)
            # Two in total: this run's archive and the newest earlier one.
            self.assertEqual([path.exists() for path in dirs], [True, False, False, False, True])
            for kept in ("elsewhere", ".repairlib-recovery-link", ".repairlib-recovery-file", "repairlib"):
                self.assertTrue((parent / kept).exists(), kept)


if __name__ == "__main__":
    unittest.main()
