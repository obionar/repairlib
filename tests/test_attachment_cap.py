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

    def test_backup_and_upload_share_one_limit(self):
        self.assertEqual(backup_archive.MAX_ATTACHMENTS, attachment_domain.MAX_RETAINED_ATTACHMENTS)


if __name__ == "__main__":
    unittest.main()
