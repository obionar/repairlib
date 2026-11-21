"""The ticket's labour charge link lives in `repair_fees`, not in audit history."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from repairlib.storage import initialization
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_feedback import form_for
from test_web_repair_workspace import panel

BOOKKEEPING = "SELECT count(*) FROM audit_events WHERE actor_kind = 'maintenance' AND operation = 'repair.fees'"


class RepairFeeTests(TokenFixture, unittest.TestCase):
    def test_labour_link_is_stored_on_the_repair_and_zero_stays_zero(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            repair = client.post(API, json=DEVICE, headers=auth | {"Idempotency-Key": uuid4().hex}).json()
            ticket = f"/repairs/{repair['id']}"
            action = ticket + "/fees"
            saved = client.post(action, data=form_for(client.get(ticket), action) | {"labor": "35.50"})
            self.assertEqual(saved.status_code, 303)
            with closing(sqlite3.connect(self.db)) as db:
                link = db.execute("SELECT labor_charge_id FROM repair_fees WHERE repair_id = ?",
                                  (repair["id"],)).fetchone()
                self.assertIsNotNone(link)
                self.assertEqual(db.execute("SELECT unit_price_cents FROM repair_charges WHERE id = ?",
                                            link).fetchone(), (3550,))
                self.assertEqual(db.execute(BOOKKEEPING).fetchone(), (0,))
            zero = client.post(action, data=form_for(client.get(ticket), action) | {"labor": "0"})
            self.assertEqual(zero.status_code, 303)
            self.assertRegex(panel(client.get(ticket), "payment-total"), r'name="labor"[^>]*value="0\.00"')
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT labor_charge_id FROM repair_fees WHERE repair_id = ?",
                                            (repair["id"],)).fetchone(), (None,))

    def test_link_must_name_a_charge_of_the_same_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repairlib.sqlite3"
            initialization.initialize_database(path, "Shop", "UTC", "owner", "hash")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (1, 1, 'A'), (2, 1, 'B')")
                db.execute("INSERT INTO repair_charges(id,shop_id,repair_id,kind,description,quantity_milli,unit,"
                           "unit_price_cents,created_by,created_by_username) "
                           "VALUES (7,1,2,'labor','Labour',1000,'h',2000,1,'owner')")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO repair_fees(shop_id, repair_id, labor_charge_id) VALUES (1, 1, 7)")

    def test_upgrade_moves_bookkeeping_rows_into_repair_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repairlib.sqlite3"
            initialization.initialize_database(path, "Shop", "UTC", "owner", "hash",
                                               target=initialization.SCHEMA_VERSION - 1)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (1, 1, 'A'), (2, 1, 'B')")
                db.execute("INSERT INTO repair_charges(id,shop_id,repair_id,kind,description,quantity_milli,unit,"
                           "unit_price_cents,created_by,created_by_username) "
                           "VALUES (7,1,1,'labor','Labour',1000,'h',2000,1,'owner')")
                rows = [(1, {"fee": "labor", "charge_id": None}), (1, {"fee": "labor", "charge_id": 7}),
                        (2, {"fee": "labor", "charge_id": None})]
                for repair_id, changes in rows:
                    db.execute("INSERT INTO audit_events(shop_id, actor_kind, operation, source, entity_type, "
                               "entity_id, changes) VALUES (1, 'maintenance', 'repair.fees', 'web', 'repair', ?, ?)",
                               (repair_id, json.dumps(changes)))
            initialization.upgrade_database(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT repair_id, labor_charge_id FROM repair_fees ORDER BY 1")
                                 .fetchall(), [(1, 7), (2, None)])
                self.assertEqual(db.execute(BOOKKEEPING).fetchone(), (0,))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
