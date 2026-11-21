"""S3 pickup independence and readable/restorable archive gates."""

import json
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret


class RepairFlagTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def create(self, client, auth):
        response = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def change(self, client, auth, repair_id, revision, stage):
        return client.post(f"{API}/{repair_id}/stage",
                           headers={**auth, "Idempotency-Key": uuid4().hex},
                           json={"revision": revision, "stage": stage, "reason": ""})
    def test_pickup_archive_restore_and_archived_write_refusal(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.create(client, auth)
            url = f"{API}/{repair['id']}"
            pickup_key = uuid4().hex
            picked = client.post(f"{url}/pickup", headers={**auth, "Idempotency-Key": pickup_key},
                                 json={"revision": 1, "enabled": True})
            self.assertEqual(picked.status_code, 200, picked.text)
            self.assertIsNotNone(picked.json()["picked_up_at"])
            self.assertEqual(picked.json()["stage"], "new")
            self.assertEqual(client.post(f"{url}/pickup",
                                         headers={**auth, "Idempotency-Key": pickup_key},
                                         json={"revision": 1, "enabled": True}).json(), picked.json())
            active = self.change(client, auth, repair["id"], 2, "in_progress")
            self.assertEqual(active.status_code, 200)
            archive_key = uuid4().hex
            archived = client.post(f"{url}/archive", headers={**auth, "Idempotency-Key": archive_key},
                                   json={"revision": 3, "enabled": True})
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertIsNotNone(archived.json()["archived_at"])
            self.assertEqual(client.get(url, headers=auth).json(), archived.json())
            self.assertEqual(self.change(client, auth, repair["id"], 4, "lab_test").status_code, 409)
            self.assertEqual(client.post(f"{url}/entries",
                                         headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={"kind": "note", "body": "Denied while archived"}).status_code,
                             409)
            restored = client.post(f"{url}/archive", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 4, "enabled": False})
            self.assertEqual(restored.status_code, 200)
            self.assertIsNone(restored.json()["archived_at"])
            entry = client.post(f"{url}/entries", headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"kind": "note", "body": "Allowed after restore"})
            self.assertEqual(entry.status_code, 201)
        operations = self.snapshot(
            "SELECT operation FROM audit_events WHERE operation IN ('repair.pickup','repair.archive') ORDER BY id"
        )
        self.assertEqual(operations, [("repair.pickup",), ("repair.archive",), ("repair.archive",)])

    def test_real_http_cli_pickup_archive_and_restore(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair = self.create(client, auth)

            def cli(*args):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            picked = cli("pickup", str(repair["id"]), "--revision", "1", "--key", uuid4().hex)
            self.assertEqual((picked["revision"], picked["stage"]), (2, "new"))
            self.assertIsNotNone(picked["picked_up_at"])
            archived = cli("archive", str(repair["id"]), "--revision", "2", "--key", uuid4().hex)
            self.assertIsNotNone(archived["archived_at"])
            restored = cli("archive", str(repair["id"]), "--revision", "3", "--undo",
                           "--key", uuid4().hex)
            self.assertIsNone(restored["archived_at"])
