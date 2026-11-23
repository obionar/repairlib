"""Reconciliation clears the two-name state of an interrupted publication, and only that (audit R13)."""

import os
import unittest

from repairlib.domain import attachments as attachment_domain
import test_attachments
from test_tokens import TokenFixture

ORIGINAL = b"original bytes"


class PublicationPairTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.client() as client:
            _, auth = test_attachments.AttachmentTests.admin_auth(self, client)
            repair = test_attachments.AttachmentTests.repair(self, client, auth)
            self.upload = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], ORIGINAL)
        self.assertEqual(self.upload.status_code, 201, self.upload.text)
        self.files = self.workspace / "state" / "files"
        (self.blob,) = test_attachments.AttachmentTests.stored_files(self)
        self.staging = self.files / ".staging"
        self.staging.mkdir(exist_ok=True, mode=0o700)

    def test_staging_name_of_a_committed_blob_goes_and_the_blob_stays(self):
        pair = self.staging / ("b" * 32 + ".part")
        os.link(self.blob, pair)
        report = attachment_domain.reconcile_files(self.workspace)
        self.assertEqual((report["removed_staging"], report["removed_orphans"], report["missing"],
                          report["mismatched"], report.get("unresolved_links", 0)), (1, 0, [], [], 0))
        self.assertFalse(pair.exists())
        self.assertEqual((self.blob.read_bytes(), self.blob.stat().st_nlink), (ORIGINAL, 1))

    def test_both_names_of_an_unacknowledged_upload_go(self):
        orphan = self.blob.parent / ("a" * 32)
        pair = self.staging / ("c" * 32 + ".part")
        orphan.write_bytes(b"never committed")
        os.link(orphan, pair)
        report = attachment_domain.reconcile_files(self.workspace)
        self.assertEqual((report["removed_staging"], report["removed_orphans"]), (1, 1))
        self.assertFalse(orphan.exists() or pair.exists())
        self.assertEqual(self.blob.read_bytes(), ORIGINAL)

    def test_an_unrelated_hard_link_is_left_alone_and_reported(self):
        outside = self.workspace / "outside.txt"
        outside.write_bytes(b"not ours")
        stray = self.staging / ("d" * 32 + ".part")
        os.link(outside, stray)
        report = attachment_domain.reconcile_files(self.workspace)
        self.assertEqual((report["removed_staging"], report["removed_orphans"], report["unresolved_links"]),
                         (0, 0, 1))
        self.assertTrue(stray.exists())
        self.assertEqual(outside.read_bytes(), b"not ours")


if __name__ == "__main__":
    unittest.main()
