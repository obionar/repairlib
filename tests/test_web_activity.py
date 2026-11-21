"""UI-4b repair activity and customer context through real adapters."""

from contextlib import closing
import re
import sqlite3
import unittest
from uuid import uuid4

from test_charges import PART
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_repair_workspace import panel, upload


class WebActivityTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def test_empty_customer_keeps_labels_and_history_starts_closed(self):
        with self.client() as client:
            auth = self.auth(client)
            customer = self.post(client, "/api/v1/customers", auth, {"name": "Synthetic blank contact"})
            page = client.get(f"/customers/{customer['id']}")
            self.assertEqual(page.status_code, 200)
            record = panel(page, "customer-record")
            for label in ("Phone", "Email", "Address", "Notes"):
                self.assertRegex(record, rf'<dt>{label}:</dt>\s*<dd[^>]*>—</dd>')
            self.assertRegex(panel(page, "customer-history"), r'<details>\s*<summary class="obi-panel-bar">')

    def post(self, client, path, auth, payload, *, status=201):
        reply = client.post(path, headers={**auth, "Idempotency-Key": uuid4().hex}, json=payload)
        self.assertEqual(reply.status_code, status, reply.text)
        return reply.json()

    def auth(self, client, username="owner", scope="write"):
        self.sign_in(client, username)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(
            client, name="Private token label", scope=scope,
        )))
        return {"Authorization": f"Bearer {token}"}

    def seed_people(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops VALUES (2, 'Other workshop')")
            db.execute("""INSERT INTO users(shop_id, username, role, password_hash)
                          SELECT 1, 'reader', 'viewer', password_hash FROM users WHERE id = 1""")
            db.execute("INSERT INTO customers(id, shop_id, name) VALUES (99, 2, 'Outside customer')")
            db.execute("INSERT INTO repairs(id, shop_id, customer_id, reported_issue) "
                       "VALUES (99, 2, 99, 'Outside repair')")

    def test_activity_is_attributed_paged_shop_scoped_and_metadata_only(self):
        self.seed_people()
        with self.client() as owner, self.client() as reader:
            auth = self.auth(owner)
            repair_id = self.post(owner, API, auth, DEVICE)["id"]
            url = f"{API}/{repair_id}"
            self.post(owner, f"{url}/entries", auth, {"kind": "work", "body": "Synthetic bench note"})
            self.post(owner, f"{url}/charges", auth, {**PART, "unit_cost_cents": 789123})
            self.assertEqual(upload(owner, auth, repair_id, b"synthetic", "activity.bin").status_code, 201)
            self.post(owner, f"{url}/stage", auth, {"revision": 1, "stage": "in_progress"}, status=200)
            self.post(owner, f"{url}/pickup", auth, {"revision": 2, "enabled": True}, status=200)
            self.post(owner, f"{url}/archive", auth, {"revision": 3, "enabled": True}, status=200)
            other_id = self.post(owner, API, auth, DEVICE)["id"]
            self.post(owner, f"{API}/{other_id}/entries", auth, {"kind": "work", "body": "Different repair"})

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE users SET username = 'renamed' WHERE id = 1")
                db.execute("""INSERT INTO audit_events(shop_id, actor_kind, operation, entity_type, entity_id)
                              VALUES (2, 'maintenance', 'foreign.event', 'repair', ?)""", (repair_id,))

            endpoint = url + "/activity"
            self.assertEqual(reader.get(endpoint).status_code, 401)
            read_auth = self.auth(reader, "reader", "read")
            first = reader.get(endpoint, headers=read_auth, params={"limit": 4})
            self.assertEqual(first.status_code, 200, first.text)
            items = first.json()["items"]
            cursor = first.json()["next_before"]
            self.assertEqual(cursor, items[-1]["id"])
            second = reader.get(endpoint, headers=read_auth, params={"limit": 4, "before": cursor})
            self.assertEqual(second.status_code, 200, second.text)
            self.assertIsNone(second.json()["next_before"])
            items += second.json()["items"]
            self.assertEqual([event["operation"] for event in items], [
                "repair.archive", "repair.pickup", "repair.stage", "attachment.create",
                "charge.create", "work_entry.create", "repair.create",
            ])
            self.assertEqual([event["id"] for event in items], sorted({e["id"] for e in items}, reverse=True))
            for event in items:
                self.assertEqual(set(event), {"id", "operation", "occurred_at", "actor_username", "source",
                                              "entity_type", "entity_id", "actor_kind"})
                self.assertEqual(event["actor_kind"], "user")
                self.assertEqual((event["actor_username"], event["source"]), ("owner", "api"))
            self.assertEqual(reader.get(f"{API}/99/activity", headers=read_auth).status_code, 404)
            for params in ({"before": 0}, {"limit": 101}, {"before": str(1 << 63)}):
                self.assertEqual(reader.get(endpoint, headers=read_auth, params=params).status_code, 422)

            page = reader.get(f"/repairs/{repair_id}")
            activity = panel(page, "activity-heading")
            self.assertRegex(activity, r'<details>\s*<summary class="obi-panel-bar"><h2 id="activity-heading">Activity</h2></summary>')
            self.assertNotIn("Show repair activity", activity)
            self.assertIn("Added charge", activity)
            self.assertIn("owner", activity)
            for private in ("789123", "7891.23", "unit_cost_cents", "Private token label", "foreign.event"):
                self.assertNotIn(private, page.text)
            self.assertEqual(reader.get("/repairs/99").status_code, 404)

    def seed_customer_history(self):
        self.seed_people()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executemany("INSERT INTO customers(id, shop_id, name) VALUES (?, 1, ?)",
                           [(1, "Customer with history"), (2, "Another customer")])
            db.executemany("""INSERT INTO repairs(id, shop_id, customer_id, stage, archived_at, reported_issue)
                              VALUES (?, 1, ?, ?, ?, 'Synthetic intake')""",
                           [(i, 1, "new" if i > 1 else "repaired", "2026-01-01" if i == 2 else None)
                            for i in range(1, 28)] + [(28, 2, "new", None)])
            db.executemany("""INSERT INTO audit_events
                              (shop_id, actor_kind, actor_user_id, actor_username, operation, source,
                               entity_type, entity_id, changes)
                              VALUES (1, 'user', 1, 'owner', 'customer.update', 'web', 'customer', 1, ?)""",
                           [('{}',)] * 26)
            db.executemany("""INSERT INTO audit_events
                              (shop_id, actor_kind, actor_user_id, actor_username, operation, source,
                               entity_type, entity_id, changes)
                              VALUES (1, 'user', 1, 'owner', 'repair.create', 'web', 'repair', 1, ?)""",
                           [('{}',)] * 26)

    def test_customer_repairs_and_history_page_independently(self):
        self.seed_customer_history()
        with self.client() as client:
            self.sign_in(client, "reader")
            customer = "/customers/1"
            first = client.get(customer)
            self.assertIn("Total repairs: 27", panel(first, "customer-record"))
            visible = panel(first, "customer-repairs")
            self.assertIn('href="/repairs/27"', visible)
            self.assertNotIn('href="/repairs/28"', visible)
            self.assertNotIn('href="/repairs/2"', visible)
            history_cursor = re.search(r'\?before=(\d+)', panel(first, "customer-history"))[1]
            older = client.get(customer, params={"before": history_cursor, "repairs_before": 3})
            self.assertIn("Total repairs: 27", panel(older, "customer-record"))
            for repair_id in (1, 2):
                self.assertIn(f'href="/repairs/{repair_id}"', panel(older, "customer-repairs"))
            self.assertIn("Archived", panel(older, "customer-repairs"))
            self.assertIn(f'?before={history_cursor}#customer-repairs', older.text)
            self.assertIn('?repairs_before=3#customer-history', older.text)
            self.assertEqual(client.get("/customers/99?repairs_before=3").status_code, 404)

            repair = client.get("/repairs/1")
            self.assertIn("Other open repairs: 25", repair.text)
            self.assertIn('href="/customers/1#customer-repairs"', repair.text)
            self.assertIn("Created repair", panel(repair, "activity-heading"))
            first_activity = panel(client.get("/repairs/1?log_before=18"), "activity-heading")
            older_link = re.search(r'href="(/repairs/1\?log_before=18&amp;activity_before=\d+#activity)"',
                                   first_activity)[1].replace("&amp;", "&")
            activity = panel(client.get(older_link), "activity-heading")
            self.assertRegex(activity, r'<details open>\s*<summary class="obi-panel-bar">')
            self.assertIn('href="/repairs/1?log_before=18&amp;show_activity=true#activity"', activity)
            newest = client.get("/repairs/1?log_before=18&show_activity=true")
            self.assertRegex(panel(newest, "activity-heading"), r'<details open>\s*<summary class="obi-panel-bar">')
