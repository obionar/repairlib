"""Warranty and shop-scoped serial history gates."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from repairlib.domain.repairs import warranty_is_active
from repairlib.storage.initialization import (
    SCHEMA_VERSION,
    initialize_database,
    schema_details,
    upgrade_database,
)
from test_tokens import ACCOUNT, TokenFixture, hidden, issued_secret

API = "/api/v1/repairs"


class WarrantyHistoryTests(TokenFixture, unittest.TestCase):
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

    def test_legacy_warranty_is_preserved_by_browser_edits_and_uses_shop_calendar(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET timezone = 'Asia/Nicosia' WHERE shop_id = 1")
        with self.client() as client:
            auth = self.auth(client)
            repair = self.create(client, auth)
            url = f"/repairs/{repair['id']}"
            page = client.get(f"{url}/edit")
            self.assertNotIn('name="warranty_until"', page.text)
            form = {name: hidden(page, name) for name in ("csrf", "key", "revision")}
            form["reported_issue"] = repair["reported_issue"]
            form["warranty_until"] = "2027-02-29"
            invalid = client.post(f"{url}/edit", data=form)
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(client.get(f"{API}/{repair['id']}", headers=auth).json()["revision"],
                             repair["revision"])
            form["warranty_until"] = "2027-01-10"
            self.assertEqual(client.post(f"{url}/edit", data=form).status_code, 303)
            self.assertEqual(client.post(f"{url}/edit", data=form).status_code, 303)
        with self.client() as client:
            auth = self.auth(client)
            saved = client.get(f"{API}/{repair['id']}", headers=auth).json()
            self.assertEqual(saved["warranty_until"], "2027-01-10")
            for now, active in (
                (datetime(2027, 1, 9, 21, 59, tzinfo=UTC), True),
                (datetime(2027, 1, 10, 21, 59, tzinfo=UTC), True),
                (datetime(2027, 1, 10, 22, 0, tzinfo=UTC), False),
            ):
                with self.subTest(now=now), patch("repairlib.domain.repairs.datetime") as clock:
                    clock.now.return_value = now
                    self.assertEqual(warranty_is_active(saved["warranty_until"], "Asia/Nicosia"), active)
            page = client.get(f"{url}/edit")
            form = {name: hidden(page, name) for name in ("csrf", "key", "revision")}
            form["reported_issue"] = "Ordinary edit"
            self.assertEqual(client.post(f"{url}/edit", data=form).status_code, 303)
            saved = client.get(f"{API}/{repair['id']}", headers=auth).json()
            self.assertEqual(saved["warranty_until"], "2027-01-10")
            self.assertNotIn("Warranty active", client.get(url).text)
            patch_fields = {"reported_issue": "Updated issue", "revision": saved["revision"]}
            updated = client.patch(f"{API}/{repair['id']}",
                                   headers={**auth, "Idempotency-Key": uuid4().hex}, json=patch_fields)
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["warranty_until"], "2027-01-10")
            cleared = client.patch(f"{API}/{repair['id']}", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={**patch_fields, "revision": updated.json()["revision"],
                                         "warranty_until": ""})
            self.assertEqual(cleared.status_code, 200)
            self.assertIsNone(cleared.json()["warranty_until"])
            self.assertNotIn("Warranty", client.get(url).text)

    def test_history_is_exact_oldest_first_and_only_for_the_signed_in_shop(self):
        with self.client() as client:
            auth = self.auth(client)
            newer = self.create(client, auth, serial="SN-0001", reported_issue="Later issue")
            older = self.create(client, auth, serial="SN-0001", reported_issue="Earlier issue")
            self.create(client, auth, serial="SN-00012", reported_issue="Different serial")
            blank = self.create(client, auth)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE repairs SET created_at = ?, stage = 'in_progress' WHERE id = ?",
                           ("2020-01-01T10:00:00.000Z", older["id"]))
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other synthetic shop')")
                db.execute("INSERT INTO repairs(shop_id, serial, reported_issue) VALUES (2, ?, ?)",
                           ("SN-0001", "Outside issue"))

            history_url = f"{API}/device-history"
            self.assertEqual(client.get(history_url, params={"serial": "SN-0001"}).status_code, 401)
            response = client.get(history_url, headers=auth, params={"serial": " SN-0001 "})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), [
                {"id": older["id"], "created_at": "2020-01-01T10:00:00.000Z",
                 "stage": "in_progress", "reported_issue": "Earlier issue"},
                {name: newer[name] for name in ("id", "created_at", "stage", "reported_issue")},
            ])
            for serial in ("", "unknown"):
                self.assertEqual(client.get(history_url, headers=auth, params={"serial": serial}).json(), [])
            new = client.get("/repairs/new", params={"customer_id": 0, "serial": "SN-0001"})
            self.assertEqual(new.status_code, 200)
            self.assertIn('name="serial" value="SN-0001"', new.text)
            self.assertLess(new.text.index("Earlier issue"), new.text.index("Later issue"))
            self.assertNotIn("Outside issue", new.text)
            self.assertNotIn("Different serial", new.text)
            page = client.get(f"/repairs/{blank['id']}")
            self.assertIn(blank["reported_issue"], page.text)
            self.assertNotIn("Device history", page.text)
            self.assertNotIn("Warranty", page.text)


class WarrantyMigrationTests(unittest.TestCase):
    def test_populated_schema_29_upgrades_to_current_schema_without_losing_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prior.sqlite3"
            initialize_database(path, "Prior", "UTC", "owner", "unused", target=29)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO customers(id, shop_id, name) VALUES (7, 1, 'Prior customer')")
                db.execute("INSERT INTO repairs(id, shop_id, customer_id, serial, reported_issue) "
                           "VALUES (22, 1, 7, 'PRIOR-22', 'Prior issue')")
                db.execute("INSERT INTO work_entries(shop_id, repair_id, author_id, author_username, kind, body) "
                           "VALUES (1, 22, 1, 'owner', 'note', 'Prior work')")
            upgrade_database(path)
            upgrade_database(path)
            self.assertEqual(schema_details(path), ("initialized", SCHEMA_VERSION))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute(
                    "SELECT id, shop_id, customer_id, serial, reported_issue, warranty_until FROM repairs"
                ).fetchall(), [(22, 1, 7, "PRIOR-22", "Prior issue", None)])
                self.assertEqual(db.execute("SELECT id, name FROM customers").fetchall(), [(7, "Prior customer")])
                self.assertEqual(db.execute("SELECT repair_id, body FROM work_entries").fetchall(), [(22, "Prior work")])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
