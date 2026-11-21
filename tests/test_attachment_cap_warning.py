"""`doctor` warns before the retained-attachment count reaches what a backup can carry."""

from pathlib import Path
import unittest

from repairlib.doctor import DoctorReport
from repairlib.lifecycle.backup_archive import MAX_ATTACHMENTS


def report(count):
    return DoctorReport(status="initialized", workspace=Path("w"), attachments_checked=count)


class AttachmentCapWarningTests(unittest.TestCase):
    def test_warns_from_eighty_percent_and_keeps_the_exit_status(self):
        threshold = MAX_ATTACHMENTS * 8 // 10
        self.assertFalse([line for line in report(threshold - 1).lines() if "backup limit" in line])
        warned = [line for line in report(threshold).lines() if "backup limit" in line]
        self.assertEqual(len(warned), 1)
        self.assertIn(f"{threshold} of {MAX_ATTACHMENTS}", warned[0])
        self.assertEqual(report(MAX_ATTACHMENTS).exit_status, report(0).exit_status)


if __name__ == "__main__":
    unittest.main()
