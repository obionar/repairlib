"""S3 immutable work-entry correction and soft-deletion history gates."""

import json
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, hidden, issued_secret


class WorkEntryHistoryTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def create(self, client, auth):
        repair = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE).json()
        entry = client.post(f"{API}/{repair['id']}/entries",
                            headers={**auth, "Idempotency-Key": uuid4().hex},
                            json={"kind": "work", "body": "Original synthetic entry"})
        self.assertEqual(entry.status_code, 201, entry.text)
        return repair, entry.json()

    def test_correction_visibility_history_replay_and_archive_readback(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair, entry = self.create(client, auth)
            url = f"{API}/{repair['id']}/entries/{entry['id']}"
            missing_reason = client.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                          json={"revision": 1, "kind": "note", "body": "Corrected",
                                                "reason": ""})
            self.assertEqual(missing_reason.status_code, 422)
            key = uuid4().hex
            corrected = client.patch(url, headers={**auth, "Idempotency-Key": key}, json={
                "revision": 1, "kind": "note", "body": "Corrected synthetic entry",
                "reason": "Clarified the synthetic observation",
            })
            self.assertEqual(corrected.status_code, 200, corrected.text)
            self.assertEqual((corrected.json()["revision"], corrected.json()["body"]),
                             (2, "Corrected synthetic entry"))
            self.assertEqual(client.patch(url, headers={**auth, "Idempotency-Key": key}, json={
                "revision": 1, "kind": "note", "body": "Corrected synthetic entry",
                "reason": "Clarified the synthetic observation",
            }).json(), corrected.json())
            deleted = client.post(f"{url}/visibility", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 2, "deleted": True, "reason": "Duplicate draft"})
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual((deleted.json()["revision"], deleted.json()["deleted"]), (3, True))
            denied = client.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex}, json={
                "revision": 3, "kind": "note", "body": "Denied", "reason": "Should not correct",
            })
            self.assertEqual(denied.status_code, 409)
            restored = client.post(f"{url}/visibility", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 3, "deleted": False, "reason": "Not a duplicate"})
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json()["deleted"], False)
            history = client.get(f"{url}/history", headers=auth)
            self.assertEqual(history.status_code, 200)
            self.assertEqual([item["revision"] for item in history.json()], [4, 3, 2, 1])
            self.assertEqual([item["reason"] for item in history.json()],
                             ["Not a duplicate", "Duplicate draft",
                              "Clarified the synthetic observation", ""])
            self.assertEqual(history.json()[-1]["body"], "Original synthetic entry")
            archived = client.post(f"{API}/{repair['id']}/archive",
                                   headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 1, "enabled": True})
            self.assertEqual(archived.status_code, 200)
            self.assertEqual(client.get(f"{url}/history", headers=auth).json(), history.json())
            self.assertEqual(client.post(f"{url}/visibility",
                                         headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={"revision": 4, "deleted": True,
                                               "reason": "Archived denial"}).status_code, 409)
        operations = self.snapshot(
            "SELECT operation FROM audit_events WHERE operation LIKE 'work_entry.%' ORDER BY id"
        )
        self.assertEqual(operations, [("work_entry.create",), ("work_entry.correct",),
                                      ("work_entry.delete",), ("work_entry.restore",)])

    def test_browser_correction_delete_restore_and_history(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair, entry = self.create(client, auth)
            base = f"/repairs/{repair['id']}/entries/{entry['id']}"
            page = client.get(f"{base}/edit")
            corrected = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                         "revision": "1", "kind": "note", "body": "Browser correction",
                         "reason": "Browser correction reason"}
            self.assertEqual(client.post(f"{base}/edit", data=corrected).status_code, 303)
            page = client.get(f"{base}/visibility")
            deleted = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                       "revision": "2", "deleted": hidden(page, "deleted"), "reason": "Browser deletion"}
            self.assertEqual(client.post(f"{base}/visibility", data=deleted).status_code, 303)
            self.assertIn("Deleted", client.get(f"/repairs/{repair['id']}").text)
            history = client.get(f"{base}/history")
            self.assertEqual(history.status_code, 200)
            self.assertIn("Original synthetic entry", history.text)
            self.assertIn("Browser correction reason", history.text)
            page = client.get(f"{base}/visibility")
            restored = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                        "revision": "3", "deleted": hidden(page, "deleted"), "reason": "Browser restoration"}
            self.assertEqual(client.post(f"{base}/visibility", data=restored).status_code, 303)
            latest = client.get(f"{API}/{repair['id']}/entries/{entry['id']}/history", headers=auth)
            self.assertEqual([item["revision"] for item in latest.json()], [4, 3, 2, 1])

    def test_real_http_cli_entry_correction_visibility_and_history(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair, entry = self.create(client, auth)

            def cli(*args):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            corrected = cli("edit-entry", str(repair["id"]), str(entry["id"]),
                            "--revision", "1", "--kind", "note", "--body", "CLI correction",
                            "--reason", "CLI correction reason", "--key", uuid4().hex)
            self.assertEqual(corrected["revision"], 2)
            deleted = cli("delete-entry", str(repair["id"]), str(entry["id"]),
                          "--revision", "2", "--reason", "CLI deletion", "--key", uuid4().hex)
            self.assertTrue(deleted["deleted"])
            restored = cli("restore-entry", str(repair["id"]), str(entry["id"]),
                           "--revision", "3", "--reason", "CLI restore", "--key", uuid4().hex)
            self.assertFalse(restored["deleted"])
            history = cli("entry-history", str(repair["id"]), str(entry["id"]))
            self.assertEqual([item["revision"] for item in history], [4, 3, 2, 1])
