"""UI-5 intake and edit: native customer picker and W2/W8 assignment rules."""

from contextlib import closing
import json
import sqlite3
import unittest
from uuid import uuid4

from repairlib.adapters.web.repairs import cursor_query, cursor_value, optional_id
from repairlib.adapters.web.security import form_int
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, hidden, issued_secret


class RepairFormParsingTests(unittest.TestCase):
    def test_cursor_redirect_and_reader_agree(self):
        for raw, parsed in (("", None), ("０１", None), ("1" * 20, None),
                            (str(1 << 63), None), ("0", 0), ("0012", 12)):
            with self.subTest(raw=raw):
                form = {"log_before": raw}
                self.assertEqual(cursor_value(form, "log_before"), parsed)
                self.assertEqual(cursor_query(form), "" if parsed is None else f"log_before={parsed}")
        self.assertIsNone(optional_id(""))
        self.assertEqual(optional_id("bad"), 0)
        self.assertEqual(form_int("bad", default=-1), -1)
        self.assertEqual(form_int("0", default=-1), 0)


class WebIntakeTests(TokenFixture, unittest.TestCase):
    def seed_choices(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("INSERT INTO shops VALUES (2, 'Other workshop')")
            db.executemany("INSERT INTO customers(id, shop_id, name, archived_at) VALUES (?, ?, ?, ?)",
                           [(i, 1, f"Local contact {i:03}", None) for i in range(1, 111)]
                           + [(111, 1, "Archived contact", "2026-01-01"), (112, 2, "Outside contact", None),
                              (113, 1, "Ωραία Οθόνη", None)])
            db.executemany("""INSERT INTO users(id, shop_id, username, role, password_hash)
                              SELECT ?, ?, ?, ?, password_hash FROM users WHERE id = 1""",
                           [(i, 1, f"tech{i:03}", "technician") for i in range(2, 105)]
                           + [(105, 2, "outsider", "technician"), (106, 1, "reader", "viewer")])

    def api_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def test_serial_lookup_preserves_draft_without_creating_repair(self):
        self.seed_choices()
        with self.client() as client:
            self.sign_in(client)
            intake = client.get("/repairs/new?customer_id=113")
            self.assertRegex(intake.text, r'<button [^>]*name="lookup_history"[^>]*formnovalidate')
            fields = {name: hidden(intake, name) for name in ("csrf", "key", "customer_id")}
            fields.update(serial="BENCH-123", notes="Do not lose this draft", technician_id="2")
            lookup = client.post("/repairs/new", data={**fields, "lookup_history": "1"})
            self.assertEqual(lookup.status_code, 200)
            self.assertIn("No previous repairs for this serial.", lookup.text)
            self.assertIn("Do not lose this draft", lookup.text)
            self.assertIn('value="BENCH-123"', lookup.text)
            self.assertNotIn('name="technician_id"', lookup.text)
            self.assertEqual(hidden(lookup, "key"), fields["key"])
            self.assertEqual(hidden(lookup, "customer_id"), "113")
            self.assertEqual(self.snapshot("SELECT id FROM repairs"), [])
            self.assertEqual(client.post("/repairs/new", data={**fields, "reported_issue": "Fault"}).status_code,
                             303)
            result = client.post("/repairs/new", data={**fields, "lookup_history": "1",
                                                     "reported_issue": "Another draft"})
            self.assertIn("Repair #1", result.text)
            self.assertIn("Another draft", result.text)
            self.assertEqual(self.snapshot("SELECT id FROM repairs"), [(1,)])

    def test_picker_pages_search_and_keeps_validated_draft(self):
        self.seed_choices()
        with self.client() as client, self.client() as viewer:
            self.sign_in(client)
            self.sign_in(viewer, "reader")
            start = client.get("/repairs/new")
            self.assertIn('name="customer_q"', start.text)
            self.assertNotIn('name="reported_issue"', start.text)
            page = client.get("/repairs/new", params={"customer_q": "local contact"})
            self.assertEqual(page.status_code, 200)
            self.assertIn("Older customers", page.text)
            later = client.get("/repairs/new", params={"customer_q": "local contact", "before": 4})
            self.assertIn("Local contact 001", later.text)
            match = client.get("/repairs/new", params={"customer_q": "ωραια οθονη"})
            self.assertIn('href="/repairs/new?customer_id=113"', match.text)
            self.assertNotIn("Archived contact", client.get("/repairs/new?customer_q=archived").text)
            self.assertNotIn("Outside contact", client.get("/repairs/new?customer_q=outside").text)
            for refused in (111, 112):
                response = client.get(f"/repairs/new?customer_id={refused}")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("contact", response.text.lower().split("<main", 1)[-1])
            self.assertEqual(viewer.get("/repairs/new?customer_q=local").status_code, 403)

            self.assertIn('href="/repairs/new?customer_id=113"', client.get("/customers/113").text)
            self.assertNotIn('/repairs/new?customer_id=', viewer.get("/customers/113").text)
            selected = client.get("/repairs/new?customer_id=113")
            self.assertIn('name="customer_id" value="113"', selected.text)
            fields = {name: hidden(selected, name) for name in ("csrf", "key", "customer_id")}
            response = client.post("/repairs/new", data={**fields, "reported_issue": " ",
                                                        "serial": "SERIAL-123", "technician_id": "2"})
            self.assertEqual(response.status_code, 422)
            self.assertIn('value="SERIAL-123"', response.text)
            self.assertNotIn('name="technician_id"', response.text)
            self.assertIn('name="customer_id" value="113"', response.text)
            self.assertEqual(hidden(response, "key"), fields["key"])
            self.assertEqual(self.snapshot("SELECT id FROM repairs"), [])
            created = client.post("/repairs/new", data={**fields, "reported_issue": "Synthetic fault"})
            self.assertEqual(created.status_code, 303)
            page = client.get(created.headers["location"])
            self.assertIn("Repair #1 created", page.text)
            self.assertIn('href="/repairs/1/label"', page.text)
            no_customer = client.get("/repairs/new?customer_id=0")
            created = client.post("/repairs/new", data={name: hidden(no_customer, name) for name in ("csrf", "key")}
                                  | {"reported_issue": "Walk-in"})
            self.assertEqual(created.status_code, 303)
            self.assertEqual(self.snapshot("SELECT customer_id FROM repairs ORDER BY id"), [(113,), (None,)])

            edit = client.get("/repairs/2/edit?customer_q=ωραια οθονη")
            self.assertIn('href="/repairs/2/edit?customer_id=113"', edit.text)
            chosen = client.get("/repairs/2/edit?customer_id=113")
            self.assertIn('<option value="113" selected', chosen.text)
            self.assertIn("Walk-in", chosen.text)
            self.assertEqual(self.snapshot("SELECT customer_id FROM repairs WHERE id = 2"), [(None,)])
            fields = {name: hidden(chosen, name) for name in ("csrf", "key", "revision")}
            edit_data = {**fields, "customer_id": 113, "reported_issue": "Walk-in"}
            self.assertEqual(client.post("/repairs/2/edit", data=edit_data).status_code, 303)
            stale = client.post("/repairs/2/edit", data={**edit_data, "key": uuid4().hex,
                                                         "reported_issue": "Older unsaved draft"})
            self.assertEqual(stale.status_code, 409)
            self.assertIn("Older unsaved draft", stale.text)
            self.assertIn('<option value="113" selected', stale.text)
            self.assertEqual(self.snapshot("SELECT customer_id, reported_issue, revision FROM repairs WHERE id = 2"),
                             [(113, "Walk-in", 2)])

    def test_new_customer_returns_to_repair_picker(self):
        with self.client() as client:
            self.sign_in(client)
            self.assertIn('href="/customers/new?next=new-repair"', client.get("/repairs/new").text)
            page = client.get("/customers/new?next=new-repair")
            fields = {name: hidden(page, name) for name in ("csrf", "key", "next")}
            invalid = client.post("/customers/new", data={**fields, "name": " "})
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(hidden(invalid, "next"), "new-repair")
            created = client.post("/customers/new", data={**fields, "name": "New intake contact"})
            self.assertEqual(created.headers["location"], "/repairs/new?customer_id=1")
            self.assertIn("New intake contact", client.get(created.headers["location"]).text)
            replay = client.post("/customers/new", data={**fields, "name": "New intake contact",
                                                        "next": "https://example.invalid/"})
            self.assertEqual(replay.headers["location"], "/customers")
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customers"), [(1,)])

    def test_w2_existing_assignments_survive_edit_and_can_be_cleared(self):
        self.seed_choices()
        with self.client() as client:
            auth = self.api_auth(client)
            for technician in (2, 104):
                with self.subTest(technician=technician):
                    created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                          json={**DEVICE, "technician_id": technician})
                    self.assertEqual(created.status_code, 201, created.text)
                    record = created.json()
                    if technician == 2:
                        with closing(sqlite3.connect(self.db)) as db, db:
                            db.execute("UPDATE users SET active = 0 WHERE id = 2")
                    route = f"/repairs/{record['id']}/edit"
                    edit = client.get(route)
                    self.assertNotIn('name="technician_id"', edit.text)
                    fields = {name: hidden(edit, name) for name in ("csrf", "key", "revision")}
                    edit_body = {**fields, "reported_issue": "Ordinary edit"}
                    saved = client.post(route, data=edit_body)
                    self.assertEqual(saved.status_code, 303, saved.text)
                    self.assertEqual(self.snapshot("SELECT technician_id FROM repairs WHERE id = ?", (record["id"],)),
                                     [(technician,)])
                    changes, = self.snapshot("SELECT changes FROM audit_events WHERE operation = 'repair.update' "
                                             "AND entity_id = ?", (record["id"],))
                    self.assertNotIn("technician_id", json.loads(changes[0]))
                    edit = client.get(route)
                    fields = {name: hidden(edit, name) for name in ("csrf", "key", "revision")}
                    refused = client.post(route, data={**edit_body, **fields, "technician_id": 105})
                    self.assertEqual(refused.status_code, 422)
                    self.assertEqual(self.snapshot("SELECT revision, technician_id FROM repairs WHERE id = ?", (record["id"],)),
                                     [(2, technician)])
                    self.assertEqual(client.post(route, data={**edit_body, **fields, "technician_id": ""}).status_code, 303)
                    self.assertEqual(self.snapshot("SELECT technician_id FROM repairs WHERE id = ?", (record["id"],)), [(None,)])

    def test_w8_matching_intake_receipt_precedes_current_assignment_validation(self):
        self.seed_choices()
        with self.client() as client:
            auth = self.api_auth(client)
            headers = {**auth, "Idempotency-Key": "original-intake"}
            payload = {**DEVICE, "customer_id": 1, "technician_id": 2}
            created = client.post(API, headers=headers, json=payload)
            self.assertEqual(created.status_code, 201, created.text)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE users SET active = 0 WHERE id = 2")
        with self.client() as client:
            replay = client.post(API, headers=headers, json=payload)
            self.assertEqual(replay.status_code, 201, replay.text)
            self.assertEqual(replay.json(), created.json())
            self.assertEqual(client.post(API, headers=headers, json={**payload, "notes": "Different"}).status_code, 409)
            refused = client.post(API, headers={**auth, "Idempotency-Key": "fresh-intake"}, json=payload)
            self.assertEqual(refused.status_code, 422)
        for table in ("repairs", "repair_requests"):
            self.assertEqual(self.snapshot(f"SELECT COUNT(*) FROM {table}"), [(1,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation = 'repair.create'"), [(1,)])
