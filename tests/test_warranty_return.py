"""Warranty-return links between an earlier repair and the device's return."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_tokens import ACCOUNT, TokenFixture, issued_secret

API = "/api/v1/repairs"


class WarrantyReturnTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def create(self, client, auth, **fields):
        response = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"reported_issue": "Synthetic issue", **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_linking_persists_without_warranty_widgets_in_either_record(self):
        with self.client() as client:
            auth = self.auth(client)
            original = self.create(client, auth, serial="SN-W1", reported_issue="First failure",
                                   warranty_until="2999-12-31")
            returned = self.create(client, auth, serial="SN-W1", reported_issue="Returned under warranty",
                                   warranty_repair_id=original["id"])
            self.assertEqual(returned["warranty_repair_id"], original["id"])
            self.assertEqual(self.snapshot("SELECT warranty_repair_id FROM repairs WHERE id = ?",
                                           (returned["id"],)), [(original["id"],)])
            new_page = client.get(f"/repairs/{returned['id']}")
            self.assertEqual(new_page.status_code, 200)
            self.assertNotIn("Warranty return", new_page.text)
            old_page = client.get(f"/repairs/{original['id']}")
            self.assertEqual(old_page.status_code, 200)
            self.assertNotIn("Warranty active", old_page.text)
            self.assertNotIn("Returned under warranty", old_page.text)
            history = client.get(f"{API}/device-history", headers=auth, params={"serial": "SN-W1"})
            self.assertEqual([row["id"] for row in history.json()], [original["id"], returned["id"]])
            self.assertEqual(set(history.json()[0]),
                             {"id", "created_at", "stage", "reported_issue"})

    def test_cross_shop_archived_and_self_links_are_refused(self):
        with self.client() as client:
            auth = self.auth(client)
            original = self.create(client, auth, serial="SN-W2", reported_issue="First failure")
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other synthetic shop')")
                db.execute("INSERT INTO repairs(shop_id, serial, reported_issue) VALUES (2, ?, ?)",
                           ("SN-W2", "Outside issue"))
            outside = self.snapshot("SELECT id FROM repairs WHERE shop_id = 2")[0][0]
            refused = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"reported_issue": "Cross-shop link",
                                        "warranty_repair_id": outside})
            missing = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"reported_issue": "Unknown link", "warranty_repair_id": 99999})
            invalid_customer = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                           json={"reported_issue": "Bad customer", "customer_id": 99999})
            self.assertEqual((invalid_customer.status_code, invalid_customer.json()["error"]["code"]),
                             (422, "validation"))
            for response in (refused, missing):
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (invalid_customer.status_code, invalid_customer.json()["error"]["code"]))
                self.assertIn("warranty_repair_id", response.json()["error"]["fields"])
            closed = self.create(client, auth, serial="SN-W3", reported_issue="Closed failure")
            archived = client.post(f"{API}/{closed['id']}/archive",
                                   headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": closed["revision"], "enabled": True})
            self.assertEqual(archived.status_code, 200, archived.text)
            stale = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"reported_issue": "Archived link",
                                      "warranty_repair_id": closed["id"]})
            self.assertEqual((stale.status_code, stale.json()["error"]["code"]),
                             (invalid_customer.status_code, invalid_customer.json()["error"]["code"]))
            self.assertIn("warranty_repair_id", stale.json()["error"]["fields"])
            self_link = client.patch(f"{API}/{original['id']}",
                                     headers={**auth, "Idempotency-Key": uuid4().hex},
                                     json={"revision": original["revision"],
                                           "reported_issue": original["reported_issue"],
                                           "warranty_repair_id": original["id"]})
            self.assertEqual((self_link.status_code, self_link.json()["error"]["code"]),
                             (invalid_customer.status_code, invalid_customer.json()["error"]["code"]))
            self.assertIn("warranty_repair_id", self_link.json()["error"]["fields"])
            self.assertEqual(self.snapshot(
                "SELECT COUNT(*) FROM repairs WHERE warranty_repair_id IS NOT NULL"), [(0,)])

    def test_later_repair_and_cycle_links_are_refused(self):
        with self.client() as client:
            auth = self.auth(client)
            first = self.create(client, auth, serial="SN-W4", reported_issue="First")
            second = self.create(client, auth, serial="SN-W4", reported_issue="Second",
                                 warranty_repair_id=first["id"])

            def patch(record, link):
                return client.patch(f"{API}/{record['id']}",
                                    headers={**auth, "Idempotency-Key": uuid4().hex},
                                    json={"revision": record["revision"],
                                          "reported_issue": record["reported_issue"],
                                          "warranty_repair_id": link})

            later = patch(first, second["id"])
            self.assertEqual(later.status_code, 422, later.text)
            self.assertIn("warranty_repair_id", later.json()["error"]["fields"])
            self.assertEqual(self.snapshot("SELECT id, warranty_repair_id FROM repairs ORDER BY id"),
                             [(first["id"], None), (second["id"], first["id"])])
            # Re-saving an unchanged valid link and linking to an earlier repair still work.
            third = self.create(client, auth, serial="SN-W4", reported_issue="Third",
                                warranty_repair_id=second["id"])
            self.assertEqual(third["warranty_repair_id"], second["id"])
            moved = patch(third, first["id"])
            self.assertEqual(moved.status_code, 200, moved.text)

    def test_cli_create_accepts_a_warranty_return(self):
        with workspace_client(self.workspace) as (client, _):
            auth = self.auth(client)
            original = self.create(client, auth, serial="SN-W4", reported_issue="Earlier failure")

            def cli(*args):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            returned = cli("create", "--key", uuid4().hex, "--serial", "SN-W4",
                           "--reported-issue", "CLI warranty return",
                           "--warranty-repair-id", str(original["id"]))
            self.assertEqual(returned["warranty_repair_id"], original["id"])
            self.assertEqual(self.snapshot("SELECT warranty_repair_id FROM repairs WHERE id = ?",
                                           (returned["id"],)), [(original["id"],)])
