"""S3 stage graph, replay and explicitly reasoned reopen gates."""

import json
import re
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, hidden, issued_secret


class RepairStageTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def create(self, client, auth):
        response = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def change(self, client, auth, repair_id, revision, stage, *, key=None, reason=""):
        return client.post(f"{API}/{repair_id}/stage",
                           headers={**auth, "Idempotency-Key": key or uuid4().hex},
                           json={"revision": revision, "stage": stage, "reason": reason})

    def test_stage_graph_reopen_reason_stale_and_replay(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.create(client, auth)
            repair_id = repair["id"]
            self.assertEqual(self.change(client, auth, repair_id, 1, "new").status_code, 409)
            invalid = self.change(client, auth, repair_id, 1, "repaired")
            self.assertEqual(invalid.status_code, 409)
            self.assertEqual(invalid.json()["error"]["code"], "invalid_transition")
            active = self.change(client, auth, repair_id, 1, "in_progress")
            self.assertEqual(active.status_code, 200, active.text)
            self.assertEqual((active.json()["stage"], active.json()["revision"]), ("in_progress", 2))
            completed = self.change(client, auth, repair_id, 2, "repaired")
            self.assertEqual(completed.status_code, 200)
            missing_reason = self.change(client, auth, repair_id, 3, "in_progress")
            self.assertEqual(missing_reason.status_code, 422)
            self.assertEqual(missing_reason.json()["error"]["fields"]["reason"],
                             "Explain why the completed repair is being reopened.")
            key = uuid4().hex
            reopened = self.change(client, auth, repair_id, 3, "in_progress",
                                   key=key, reason="Customer reported the same synthetic symptom")
            self.assertEqual(reopened.status_code, 200, reopened.text)
            self.assertEqual((reopened.json()["stage"], reopened.json()["revision"]), ("in_progress", 4))
            self.assertEqual(self.change(client, auth, repair_id, 3, "in_progress", key=key,
                                         reason="Customer reported the same synthetic symptom").json(),
                             reopened.json())
            stale = self.change(client, auth, repair_id, 3, "in_progress",
                                reason="Customer reported the same synthetic symptom")
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(client.get(f"{API}/{repair_id}", headers=auth).json(), reopened.json())
        self.assertEqual(self.snapshot(
            "SELECT operation,changes FROM audit_events WHERE operation='repair.stage' ORDER BY id"
        ), [("repair.stage", json.dumps({"stage": {"before": "new", "after": "in_progress"}})),
            ("repair.stage", json.dumps({"stage": {"before": "in_progress", "after": "repaired"}})),
            ("repair.stage", json.dumps({"stage": {"before": "repaired", "after": "in_progress"},
                                         "reopen_reason": "Customer reported the same synthetic symptom"}))])

    def test_browser_reopens_and_replays_without_a_reason(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.create(client, auth)

            def form(revision, stage):
                page = client.get(f"/repairs/{repair['id']}")
                self.assertEqual(page.status_code, 200)
                stage_form = re.search(
                    rf'<form method="post" action="/repairs/{repair["id"]}/stage".*?</form>',
                    page.text, re.DOTALL,
                ).group()
                self.assertNotIn('name="reason"', stage_form)
                values = dict(re.findall(r'name="(csrf|key)" value="([^"]+)"', stage_form))
                return {**values, "revision": str(revision), "stage": stage}

            url = f"/repairs/{repair['id']}/stage"
            self.assertEqual(client.post(url, data=form(1, "in_progress")).status_code, 303)
            self.assertEqual(client.post(url, data=form(2, "repaired")).status_code, 303)
            reopen = form(3, "in_progress")
            self.assertEqual(client.post(url, data=reopen).status_code, 303)
            self.assertEqual(client.post(url, data=reopen).status_code, 303)
            page = client.get(f"/repairs/{repair['id']}")
            self.assertIn("In Progress", page.text)
            self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                           (repair["id"],)), [("in_progress", 4)])

        audit = self.snapshot("SELECT changes FROM audit_events WHERE operation='repair.stage' ORDER BY id DESC LIMIT 1")
        self.assertEqual(json.loads(audit[0][0]), {"stage": {"before": "repaired", "after": "in_progress"}})

    def test_real_http_cli_stage_and_retry(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair = self.create(client, auth)

            def cli(*args, code=0):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None

            key = uuid4().hex
            changed = cli("stage", str(repair["id"]), "--revision", "1", "--stage", "waiting_parts",
                          "--key", key)
            self.assertEqual((changed["stage"], changed["revision"]), ("waiting_parts", 2))
            self.assertEqual(cli("stage", str(repair["id"]), "--revision", "1",
                                 "--stage", "waiting_parts", "--key", key), changed)
            cli("stage", str(repair["id"]), "--revision", "1", "--stage", "lab_test",
                "--key", uuid4().hex, code=7)
            self.assertEqual(cli("show", str(repair["id"])), changed)
