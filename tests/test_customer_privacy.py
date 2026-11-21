"""PRIV-1 gate: anonymize a customer and export one customer, on synthetic data only."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest import TestCase
from uuid import uuid4

from repairlib.client import local

from test_baseline import python_env
from test_charges import LABOR, PART
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import ACCOUNT, PASSWORD, TokenFixture, issued_secret

CUSTOMERS = "/api/v1/customers"
REPAIRS = "/api/v1/repairs"
PII = {"name": "Zephyr Quillfeather", "phone": "+357 99 123456", "email": "zephyr.q@example.test",
       "address": "17 Mulberry Lane Larnaca", "notes": "Prefers calls after Vesper hour"}
EDITED_NAME = "Zephyr Quillfeather-Okonkwo"
OTHER = {"name": "Brindle Harcastle", "phone": "+357 99 654321", "email": "brindle@example.test",
         "address": "3 Cobalt Row Paphos", "notes": "Other customer note"}
ERASED = (*PII.values(), EDITED_NAME, "123456", "Quillfeather", "Mulberry")
TABLES = ("repairs", "repair_charges", "work_entries", "attachments")


class PrivacyFixture(TokenFixture):
    def setUp(self):
        super().setUp()
        with self.client() as client:
            self.sign_in(client)
            self.auth = {"Authorization": "Bearer " + issued_secret(
                client.post(ACCOUNT, data=self.issue_form(client)))}
            self.target = self.post(client, CUSTOMERS, PII)
            self.other = self.post(client, CUSTOMERS, OTHER)
            patched = client.patch(f"{CUSTOMERS}/{self.target['id']}", headers=self.key(), json={
                "revision": 1, "name": EDITED_NAME})
            self.assertEqual(patched.status_code, 200, patched.text)
            self.target_repairs = [self.post(client, REPAIRS, {**DEVICE, "customer_id": self.target["id"],
                                                               "notes": f"Synthetic repair {n}"})
                                   for n in range(2)]
            self.other_repair = self.post(client, REPAIRS, {**DEVICE, "customer_id": self.other["id"],
                                                            "notes": "Other customer repair"})
            for repair in self.target_repairs:
                self.post(client, f"{REPAIRS}/{repair['id']}/charges", PART)
                self.post(client, f"{REPAIRS}/{repair['id']}/charges", LABOR)
            self.post(client, f"{REPAIRS}/{self.other_repair['id']}/charges",
                      {**PART, "description": "Other customer part"})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside')")
            db.execute("INSERT INTO customers(shop_id, name, phone, email) VALUES (2, 'Outside person', "
                       "'+357 99 000000', 'outside@example.test')")
            self.outside_id = db.execute("SELECT id FROM customers WHERE shop_id = 2").fetchone()[0]
            db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                       "SELECT 1, 'tech', 'technician', password_hash FROM users WHERE id = 1")
            db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                       "SELECT 1, 'viewer', 'viewer', password_hash FROM users WHERE id = 1")

    def key(self, auth=None):
        return {**(auth or self.auth), "Idempotency-Key": uuid4().hex}

    def post(self, client, path, body):
        response = client.post(path, headers=self.key(), json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def token_for(self, _client, username, **overrides):
        with self.client() as session:
            self.sign_in(session, username)
            return {"Authorization": "Bearer " + issued_secret(
                session.post(ACCOUNT, data=self.issue_form(session, **overrides)))}

    def business_state(self):
        return {table: self.snapshot(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES} | {
            "customer_ids": self.snapshot("SELECT id, shop_id FROM customers ORDER BY id"),
            "repair_links": self.snapshot("SELECT id, customer_id FROM repairs ORDER BY id"),
        }

    def audit_text(self):
        return json.dumps(self.snapshot("SELECT * FROM audit_events") + self.snapshot(
            "SELECT * FROM customer_requests"), ensure_ascii=False)

    def anonymize(self, client, auth, customer_id=None, revision=None, key=None):
        customer_id = customer_id or self.target["id"]
        if revision is None:
            revision = client.get(f"{CUSTOMERS}/{customer_id}", headers=auth).json()["revision"]
        return client.post(f"{CUSTOMERS}/{customer_id}/anonymize", json={"revision": revision},
                           headers={**auth, "Idempotency-Key": key or uuid4().hex})


class AnonymizeTests(PrivacyFixture, TestCase):
    def test_anonymize_erases_personal_data_and_keeps_every_business_record(self):
        before = self.business_state()
        audit_ids = [row[0] for row in self.snapshot("SELECT id FROM audit_events ORDER BY id")]
        with self.client() as client:
            charges = client.get(f"{REPAIRS}/{self.target_repairs[0]['id']}/charges", headers=self.auth).json()
            response = self.anonymize(client, self.auth)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["id"], self.target["id"])
            for field in ("name", "phone", "email", "address", "notes"):
                self.assertNotEqual(result[field], PII[field])
            self.assertTrue(result["name"].strip())
            self.assertEqual((result["phone"], result["email"], result["address"], result["notes"]),
                             ("", "", "", ""))
            reread = client.get(f"{CUSTOMERS}/{self.target['id']}", headers=self.auth).json()
            self.assertEqual({k: reread[k] for k in PII}, {k: result[k] for k in PII})
            self.assertEqual(client.get(f"{REPAIRS}/{self.target_repairs[0]['id']}/charges",
                                        headers=self.auth).json(), charges)
            listing = client.get(f"{REPAIRS}?customer_id={self.target['id']}", headers=self.auth).json()
            self.assertEqual(len(listing["items"]), 2)
            self.assertNotIn("Quillfeather", json.dumps(listing))
        self.assertEqual(self.business_state(), before)
        self.assertEqual(self.snapshot("SELECT phone_digits FROM customers WHERE id = ?",
                                       (self.target["id"],)), [("",)])
        self.assertEqual([row[0] for row in self.snapshot(
            "SELECT id FROM audit_events WHERE id <= ? ORDER BY id", (audit_ids[-1],))], audit_ids)

    def test_other_customers_and_other_shop_are_untouched(self):
        other_row = self.snapshot("SELECT * FROM customers WHERE id = ?", (self.other["id"],))
        outside_row = self.snapshot("SELECT * FROM customers WHERE id = ?", (self.outside_id,))
        with self.client() as client:
            refused = self.anonymize(client, self.auth, self.outside_id, revision=1)
            self.assertEqual(refused.status_code, 404)
            self.assertEqual(self.anonymize(client, self.auth, 99999, revision=1).status_code, 404)
            self.assertEqual(self.anonymize(client, self.auth).status_code, 200)
        self.assertEqual(self.snapshot("SELECT * FROM customers WHERE id = ?", (self.other["id"],)), other_row)
        self.assertEqual(self.snapshot("SELECT * FROM customers WHERE id = ?", (self.outside_id,)), outside_row)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation = 'customer.anonymize'"),
                         [(1,)])

    def test_non_admin_and_read_only_tokens_are_refused_without_side_effects(self):
        with self.client() as client:
            refused = [self.token_for(client, "tech"), self.token_for(client, "viewer", scope="read"),
                       self.token_for(client, "owner", scope="read")]
            before = self.business_state(), self.snapshot("SELECT * FROM customers ORDER BY id"), self.audit_text()
            for auth in refused:
                response = self.anonymize(client, auth, revision=2)
                self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(client.post(f"{CUSTOMERS}/{self.target['id']}/anonymize", json={"revision": 2},
                                         headers={"Idempotency-Key": uuid4().hex}).status_code, 401)
        self.assertEqual((self.business_state(), self.snapshot("SELECT * FROM customers ORDER BY id"),
                          self.audit_text()), before)

    def test_stale_revision_is_refused_and_replay_returns_the_first_result(self):
        with self.client() as client:
            self.assertEqual(self.anonymize(client, self.auth, revision=1).status_code, 409)
            self.assertEqual(self.snapshot("SELECT name FROM customers WHERE id = ?", (self.target["id"],)),
                             [(EDITED_NAME,)])
            first = self.anonymize(client, self.auth, key="anon-once")
            self.assertEqual(first.status_code, 200)
            replay = self.anonymize(client, self.auth, revision=2, key="anon-once")
            self.assertEqual((replay.status_code, replay.json()), (200, first.json()))
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation = 'customer.anonymize'"),
                         [(1,)])

    def test_second_anonymize_is_a_clear_refusal_with_no_new_event(self):
        with self.client() as client:
            first = self.anonymize(client, self.auth).json()
            again = self.anonymize(client, self.auth)
            self.assertEqual(again.status_code, 409, again.text)
            self.assertEqual(again.json()["error"]["code"], "conflict")
            self.assertIn("anonymized", again.json()["error"]["message"].lower())
            self.assertEqual(client.get(f"{CUSTOMERS}/{self.target['id']}", headers=self.auth).json(), first)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation = 'customer.anonymize'"),
                         [(1,)])

    def test_audit_records_who_and_when_and_no_erased_value_survives_anywhere_in_audit(self):
        history_before = None
        with self.client() as client:
            history_before = client.get(f"{CUSTOMERS}/{self.target['id']}/history", headers=self.auth).json()
            self.assertIn("Quillfeather", json.dumps(history_before))
            self.assertEqual(self.anonymize(client, self.auth).status_code, 200)
            history = client.get(f"{CUSTOMERS}/{self.target['id']}/history", headers=self.auth).json()
        event = history["items"][0]
        self.assertEqual((event["operation"], event["actor_username"], event["source"]),
                         ("customer.anonymize", "owner", "api"))
        self.assertTrue(event["occurred_at"].endswith("Z"))
        self.assertEqual([item["id"] for item in history["items"][1:]],
                         [item["id"] for item in history_before["items"]])
        self.assertEqual([item["operation"] for item in history["items"][1:]],
                         [item["operation"] for item in history_before["items"]])
        text = self.audit_text()
        for value in ERASED:
            self.assertNotIn(value, text)
        self.assertIn(OTHER["name"], json.dumps(self.snapshot("SELECT name FROM customers")))

    def test_cli_anonymize_is_an_admin_action_with_local_attribution(self):
        base = [*COMMAND, "customers"]
        result = subprocess.run(
            [*base, "anonymize", str(self.target["id"]), "--workspace", str(self.workspace),
             "--revision", "2", "--key", "cli-anon", "--json"],
            capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phone"], "")
        self.assertEqual(self.snapshot("SELECT source, actor_username FROM audit_events "
                                       "WHERE operation = 'customer.anonymize'"), [("cli", "cli")])
        again = subprocess.run(
            [*base, "anonymize", str(self.target["id"]), "--workspace", str(self.workspace),
             "--revision", "3", "--key", "cli-anon-2"],
            capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertNotEqual(again.returncode, 0)
        for value in ERASED:
            self.assertNotIn(value, result.stdout + result.stderr + again.stdout + again.stderr)

    def test_history_page_keeps_redacted_events_for_the_domain_reader(self):
        with self.client() as client:
            self.assertEqual(self.anonymize(client, self.auth).status_code, 200)
        ctx = local.open_workspace(str(self.workspace))
        with self.assertRaises(local.ClientError):
            local.customer_history(ctx, 99999, limit=25, before=None)
        page = local.customer_history(ctx, self.target["id"], limit=25, before=None)
        self.assertEqual([item.operation for item in page.items],
                         ["customer.anonymize", "customer.update", "customer.create"])


class ExportTests(PrivacyFixture, TestCase):
    def export(self, client, auth, customer_id=None):
        return client.get(f"{CUSTOMERS}/{customer_id or self.target['id']}/export", headers=auth)

    def test_api_export_round_trips_the_customer_repairs_and_charges_only(self):
        with self.client() as client:
            document = self.export(client, self.auth)
            self.assertEqual(document.status_code, 200, document.text)
            data = document.json()
            customer = client.get(f"{CUSTOMERS}/{self.target['id']}", headers=self.auth).json()
            self.assertEqual({k: data["customer"][k] for k in ("id", "revision", *PII)},
                             {k: customer[k] for k in ("id", "revision", *PII)})
            self.assertEqual(data["customer"]["name"], EDITED_NAME)
            self.assertEqual([item["id"] for item in data["repairs"]],
                             sorted(r["id"] for r in self.target_repairs))
            for item in data["repairs"]:
                repair = client.get(f"{REPAIRS}/{item['id']}", headers=self.auth).json()
                self.assertEqual({k: v for k, v in item.items() if k != "charges"}, repair)
                self.assertEqual(item["customer_id"], self.target["id"])
                summary = client.get(f"{REPAIRS}/{item['id']}/charges", headers=self.auth).json()
                self.assertEqual([{k: v for k, v in charge.items() if k in summary["items"][0]}
                                  for charge in item["charges"]], summary["items"])
                self.assertEqual(len(item["charges"]), 2)
            self.assertNotIn(OTHER["name"], document.text)
            self.assertNotIn("Other customer", document.text)
            self.assertNotIn("Outside", document.text)
            self.assertEqual(json.loads(document.text), data)

    def test_export_authorization_matches_reading_the_customer(self):
        with self.client() as client:
            self.assertEqual(client.get(f"{CUSTOMERS}/{self.target['id']}/export").status_code, 401)
            for auth in (self.token_for(client, "tech"), self.token_for(client, "viewer", scope="read"),
                         self.token_for(client, "owner", scope="read")):
                self.assertEqual(client.get(f"{CUSTOMERS}/{self.target['id']}", headers=auth).status_code, 200)
                self.assertEqual(self.export(client, auth).status_code, 200)
            viewer = self.token_for(client, "viewer", scope="read")
            costs = [charge["unit_cost_cents"] for repair in self.export(client, viewer).json()["repairs"]
                     for charge in repair["charges"]]
            self.assertEqual(costs, [None] * 4)
            self.assertEqual(self.export(client, self.auth, self.outside_id).status_code, 404)
            self.assertEqual(self.export(client, self.auth, 99999).status_code, 404)

    def test_export_has_no_side_effects_and_covers_an_anonymized_customer(self):
        before = self.snapshot("SELECT COUNT(*) FROM audit_events")
        with self.client() as client:
            self.export(client, self.auth)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events"), before)
            self.assertEqual(self.anonymize(client, self.auth).status_code, 200)
            data = self.export(client, self.auth).json()
        self.assertEqual(data["customer"]["phone"], "")
        self.assertEqual(len(data["repairs"]), 2)
        self.assertNotIn("Quillfeather", json.dumps(data))

    def test_cli_export_prints_the_same_json_document(self):
        with self.client() as client:
            api = self.export(client, self.auth).json()
        result = subprocess.run(
            [*COMMAND, "customers", "export", str(self.target["id"]), "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), api)
        missing = subprocess.run(
            [*COMMAND, "customers", "export", "99999", "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, "")


if __name__ == "__main__":
    import unittest
    unittest.main()
