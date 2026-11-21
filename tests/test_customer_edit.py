"""Customer edit gates for reviewed revisions, retries and refused writes."""

from contextlib import closing
from dataclasses import asdict
import json
import sqlite3
import subprocess
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx

from repairlib.domain import customers
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_customers import API, FIELDS
from test_tokens import ACCOUNT, TokenFixture, hidden, issued_secret


class CustomerEditTests(TokenFixture, TestCase):
    def create(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        auth = {"Authorization": f"Bearer {token}"}
        response = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=FIELDS)
        self.assertEqual(response.status_code, 201)
        return token, auth, response.json()

    def test_two_clients_preserve_stale_draft_then_replay_before_revision_check(self):
        with self.client() as first, self.client() as second:
            _, auth, original = self.create(first)
            self.sign_in(second)
            url = f"{API}/{original['id']}"
            edit = f"/customers/{original['id']}/edit"
            self.assertEqual(first.get(url, headers=auth).json(), original)
            page = second.get(edit)
            self.assertEqual(page.status_code, 200)
            form = {**FIELDS, "name": "Browser draft", "csrf": hidden(page, "csrf"),
                    "key": hidden(page, "key"), "revision": hidden(page, "revision")}
            headers = {**auth, "Idempotency-Key": uuid4().hex}
            committed = first.patch(url, headers=headers, json={"revision": 1, "phone": "Updated"})
            self.assertEqual(committed.status_code, 200)
            self.assertEqual(committed.json()["revision"], 2)

            stale = second.post(edit, data=form)
            self.assertEqual(stale.status_code, 409)
            self.assertIn("Browser draft", stale.text)
            self.assertEqual(hidden(stale, "key"), form["key"])
            self.assertEqual(hidden(stale, "revision"), form["revision"])
            self.assertEqual(first.get(url, headers=auth).json()["name"], FIELDS["name"])
            fresh = second.get(edit)
            self.assertNotEqual(hidden(fresh, "key"), form["key"])
            reviewed = {**form, "key": hidden(fresh, "key"), "revision": hidden(fresh, "revision"),
                        "phone": "Updated"}
            self.assertEqual(second.post(edit, data=reviewed).status_code, 303)
            self.assertEqual(second.post(edit, data=reviewed).status_code, 303)
            replay = first.patch(url, headers=headers, json={"revision": 1, "phone": "Updated"})
            self.assertEqual(replay.json(), committed.json())
            self.assertEqual(first.patch(url, headers=headers,
                                         json={"revision": 2, "phone": "Updated"}).status_code, 409)
            final = first.get(url, headers=auth).json()
            self.assertEqual((final["revision"], final["name"], final["phone"]),
                             (3, "Browser draft", "Updated"))

        rows = self.snapshot("SELECT operation, source, changes FROM audit_events "
                             "WHERE operation='customer.update' ORDER BY id")
        self.assertEqual([(operation, source) for operation, source, _ in rows],
                         [("customer.update", "api"), ("customer.update", "web")])
        self.assertEqual(json.loads(rows[0][2]), {"phone": {"before": FIELDS["phone"], "after": "Updated"}})

    def test_edit_denials_and_replay_after_disconnection(self):
        with self.client() as client:
            _, auth, original = self.create(client)
            key = uuid4().hex
            headers = {**auth, "Idempotency-Key": key}
            url = f"{API}/{original['id']}"
            edit_input = {"revision": 1, "address": "New street"}
            missing_key = client.patch(url, headers=auth, json=edit_input)
            self.assertEqual(missing_key.status_code, 422)
            self.assertEqual(missing_key.json()["error"]["code"], "validation")

            def disconnect(*args, **kwargs):
                nonlocal retained
                retained = customers.update_customer(*args, **kwargs)
                raise httpx.ReadError("Synthetic lost edit response")

            retained = None
            with patch("repairlib.adapters.api.customers.update_customer", disconnect):
                self.assertEqual(client.patch(url, headers=headers, json=edit_input).status_code, 500)
            reader = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
            denied = client.patch(url, headers={**headers, "Authorization": f"Bearer {reader}"},
                                  json=edit_input)
            self.assertEqual(denied.status_code, 403)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO customers(id,shop_id,name) VALUES (2,2,'Hidden')")
            self.assertEqual(client.get(f"{API}/2", headers=auth).status_code, 404)
            self.assertEqual(client.patch(f"{API}/2", headers=headers, json=edit_input).status_code, 404)
            self.assertEqual(client.get("/customers/2/edit").status_code, 404)
        with self.client() as client:
            replay = client.patch(url, headers=headers, json=edit_input)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json(), asdict(retained))
        self.assertEqual(self.snapshot("SELECT id,revision FROM customers ORDER BY id"), [(1, 2), (2, 1)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='customer.update'"),
                         [(1,)])

    def test_cli_show_edit_and_stale_refusal_over_http(self):
        with workspace_client(self.workspace) as (client, _):
            token, _, original = self.create(client)

            def cli(*args, stdin=None, code=0):
                result = subprocess.run([*COMMAND, "customers", *args, "--workspace", str(self.workspace), "--json"],
                                        input=stdin, text=True, capture_output=True, timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None

            record_id = str(original["id"])
            self.assertEqual(cli("show", record_id), original)
            key = uuid4().hex
            edited = cli("edit", record_id, "--revision", "1", "--key", key, "--notes", "")
            self.assertEqual((edited["revision"], edited["notes"], edited["phone"]),
                             (2, "", FIELDS["phone"]))
            cli("edit", record_id, "--revision", "1", "--key", uuid4().hex,
                "--notes", "stale", code=7)
            self.assertEqual(cli("show", record_id), edited)
