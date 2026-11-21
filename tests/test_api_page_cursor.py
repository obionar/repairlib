"""A malformed page cursor names the resource the caller asked for."""

import unittest

import test_attachments
from test_tokens import TokenFixture

ENDPOINTS = {
    "/api/v1/customers": "Invalid customer page.",
    "/api/v1/repairs": "Invalid repair page.",
    "/api/v1/users": "Invalid user page.",
    "/api/v1/inventory": "Invalid inventory page.",
}


class ApiPageCursorTests(TokenFixture, unittest.TestCase):
    def test_bad_cursor_message_names_its_resource(self):
        with self.client() as client:
            _, auth = test_attachments.AttachmentTests.admin_auth(self, client)
            for path, message in ENDPOINTS.items():
                reply = client.get(path, params={"before": "abc"}, headers=auth)
                self.assertEqual((path, reply.status_code, reply.json()["error"]["message"]),
                                 (path, 422, message))


if __name__ == "__main__":
    unittest.main()
