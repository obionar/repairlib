"""S3 printable repair summaries and labels: no JavaScript, no costs, shop-scoped."""

from contextlib import closing
import sqlite3
import unittest
from uuid import uuid4

from test_charges import LABOR, PART
from test_repairs import API, CUSTOMERS, DEVICE
from test_tokens import PASSWORD, TokenFixture
from test_users import ACCOUNT, issued_secret


class PrintingTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def test_print_limit_counts_only_live_entries_and_marks_truncation(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO repairs(shop_id, reported_issue) VALUES (1, 'Print fixture')")
            sql = """INSERT INTO work_entries(shop_id, repair_id, author_id, author_username, kind, body, deleted_at)
                     VALUES (1, 1, 1, 'owner', 'note', ?, ?)"""
            db.execute(sql, ("Old live entry", None))
            db.executemany(sql, [("Deleted confidential text", "2026-01-01")] * 200)
        with self.client() as client:
            self.sign_in(client)
            first = client.get("/repairs/1/print").text
            self.assertIn("Old live entry", first)
            self.assertNotIn("Deleted confidential text", first)
            self.assertNotIn("Showing the latest", first)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.executemany(sql, [(f"Live note {i:03d}", None) for i in range(200)])
            excerpt = client.get("/repairs/1/print").text
            self.assertIn("Showing the latest 200 work entries.", excerpt)
            self.assertNotIn("Old live entry", excerpt)
            self.assertNotIn("Deleted confidential text", excerpt)
            self.assertEqual(excerpt.count("Live note "), 200)
            self.assertLess(excerpt.index("Live note 000"), excerpt.index("Live note 199"))

    def test_summary_and_label_print_views(self):
        with self.client() as client, self.client() as viewer:
            _, auth = self.admin_auth(client)
            customer = client.post(CUSTOMERS, headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"name": "Πελάτης Δοκιμής", "phone": "+357 555 0102"}).json()
            repair = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                 json={**DEVICE, "customer_id": customer["id"]}).json()
            for charge in (PART, LABOR):
                client.post(f"{API}/{repair['id']}/charges", json=charge,
                            headers={**auth, "Idempotency-Key": uuid4().hex})
            client.post(f"{API}/{repair['id']}/entries", headers={**auth, "Idempotency-Key": uuid4().hex},
                        json={"kind": "work", "body": "Visible synthetic work"})
            hidden_entry = client.post(f"{API}/{repair['id']}/entries",
                                       headers={**auth, "Idempotency-Key": uuid4().hex},
                                       json={"kind": "note", "body": "Deleted synthetic note"}).json()
            client.post(f"{API}/{repair['id']}/entries/{hidden_entry['id']}/visibility",
                        headers={**auth, "Idempotency-Key": uuid4().hex},
                        json={"revision": 1, "deleted": True, "reason": "Draft"})

            summary = client.get(f"/repairs/{repair['id']}/print")
            self.assertEqual(summary.status_code, 200)
            for text in ("Synthetic", f"Repair {repair['id']}", "Πελάτης Δοκιμής", "+357 555 0102",
                         "Does not power on", "Visible synthetic work", "39.99"):
                self.assertIn(text, summary.text)
            self.assertNotIn("Deleted synthetic note", summary.text)
            self.assertNotIn("8.00", summary.text)  # unit cost is never printed
            self.assertNotIn("<script", summary.text)
            self.assertIn("print.css", summary.text)
            self.assertIn(f'href="/repairs/{repair["id"]}/print"', client.get(f"/repairs/{repair['id']}").text)

            label = client.get(f"/repairs/{repair['id']}/label")
            self.assertEqual(label.status_code, 200)
            for text in (f"#{repair['id']}", "Πελάτης Δοκιμής", "Laptop", "SN-001"):
                self.assertIn(text, label.text)
            self.assertNotIn("39.99", label.text)
            css = client.get("/static/print.css")
            self.assertEqual(css.status_code, 200)
            self.assertIn("@media print", css.text)
            self.assertIn("@page", css.text)

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
            self.assertEqual(client.get("/repairs/50/print").status_code, 404)
            self.assertEqual(client.get("/repairs/50/label").status_code, 404)
            self.sign_in(viewer, "viewer")
            self.assertEqual(viewer.get(f"/repairs/{repair['id']}/print").status_code, 200)
            anonymous = self.client()
            with anonymous:
                self.assertEqual(anonymous.get(f"/repairs/{repair['id']}/print").status_code, 303)


if __name__ == "__main__":
    unittest.main()
