"""The retained-attachment count stays inside what backup and update can carry."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from repairlib.domain import attachments as attachment_domain
from repairlib.lifecycle import backup_archive
import test_attachments
from test_tokens import TokenFixture


class AttachmentCapTests(TokenFixture, unittest.TestCase):
    def test_upload_past_the_retained_cap_is_refused_and_replay_still_works(self):
        with self.client() as client:
            _, auth = test_attachments.AttachmentTests.admin_auth(self, client)
            repair = test_attachments.AttachmentTests.repair(self, client, auth)
            with patch.object(attachment_domain, "MAX_RETAINED_ATTACHMENTS", 2):
                first_key = uuid4().hex
                first = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"one", key=first_key)
                second = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"two")
                self.assertEqual((first.status_code, second.status_code), (201, 201))
                refused = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"three")
                replayed = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"one", key=first_key)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "attachment_limit")
            self.assertEqual(len(test_attachments.AttachmentTests.stored_files(self)), 2)  # the refused bytes were not kept
            self.assertEqual(replayed.status_code, 201)
            self.assertEqual(replayed.json()["id"], first.json()["id"])

    def test_retained_bytes_including_detached_files_stay_inside_the_backup_payload(self):
        with self.client() as client:
            _, auth = test_attachments.AttachmentTests.admin_auth(self, client)
            repair = test_attachments.AttachmentTests.repair(self, client, auth)
            with patch.object(attachment_domain, "MAX_RETAINED_BYTES", 10):
                first_key = uuid4().hex
                first = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"123456", key=first_key)
                self.assertEqual(first.status_code, 201, first.text)
                detached = client.post(f"/api/v1/repairs/{repair['id']}/attachments/{first.json()['id']}/detach",
                                       headers={**auth, "Idempotency-Key": uuid4().hex},
                                       json={"revision": 1, "detached": True})
                self.assertEqual(detached.status_code, 200, detached.text)
                refused = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"abcde")
                fits = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"abcd")
                replayed = test_attachments.AttachmentTests.upload(self, client, auth, repair["id"], b"123456", key=first_key)
            self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (409, "attachment_limit"))
            self.assertEqual((fits.status_code, replayed.status_code), (201, 201))
            self.assertEqual(len(test_attachments.AttachmentTests.stored_files(self)), 2)

    def test_upload_limits_leave_headroom_for_the_database_in_a_backup(self):
        self.assertEqual(attachment_domain.MAX_RETAINED_BYTES + attachment_domain.DATABASE_HEADROOM,
                         backup_archive.MAX_PAYLOAD)

    def test_backup_and_upload_share_one_limit(self):
        self.assertEqual(backup_archive.MAX_ATTACHMENTS, attachment_domain.MAX_RETAINED_ATTACHMENTS)


if __name__ == "__main__":
    unittest.main()
