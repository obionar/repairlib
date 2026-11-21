"""The ticket's diagnosis and labour charge links live in `repair_fees`, not in audit history."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from uuid import uuid4

from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_feedback import form_for
from test_web_repair_workspace import panel

BOOKKEEPING = "SELECT count(*) FROM audit_events WHERE actor_kind = 'maintenance' AND operation = 'repair.fees'"


class RepairFeeTests(TokenFixture, unittest.TestCase):
    def test_diagnosis_and_repair_are_prefilled_stored_and_match_the_cli(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            repair = client.post(API, json=DEVICE, headers=auth | {"Idempotency-Key": uuid4().hex}).json()
            ticket = f"/repairs/{repair['id']}"
            action = ticket + "/fees"
            page = client.get(ticket)
            form = panel(page, "payment-total")
            # The bench diagnosis defaults from the shop settings; the repair starts at zero.
            self.assertRegex(form, r'name="diagnosis"[^>]*value="20\.00"')
            self.assertRegex(form, r'name="labor"[^>]*value="0\.00"')
            self.assertRegex(form, r'id="fee-parts"[^>]*readonly')
            saved = client.post(action, data=form_for(page, action) | {
                "diagnosis": "20.00", "labor": "35.50", "discount": "0"})
            self.assertEqual(saved.status_code, 303)
            total = panel(client.get(ticket), "payment-total")
            self.assertIn("55.50", total)
            with closing(sqlite3.connect(self.db)) as db:
                link = db.execute("SELECT diagnosis_charge_id, labor_charge_id FROM repair_fees "
                                  "WHERE repair_id = ?", (repair["id"],)).fetchone()
                self.assertIsNotNone(link[0])
                self.assertIsNotNone(link[1])
                self.assertEqual([db.execute("SELECT unit_price_cents FROM repair_charges WHERE id = ?",
                                             (charge_id,)).fetchone()[0] for charge_id in link], [2000, 3550])
                self.assertEqual(db.execute(BOOKKEEPING).fetchone(), (0,))
            result = subprocess.run([*COMMAND, "repairs", "charges", str(repair["id"]),
                                     "--workspace", str(self.workspace)],
                                    text=True, capture_output=True, timeout=20, env=python_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Diagnosis 20.00, repair 35.50", result.stdout)
            self.assertIn("total 55.50", result.stdout)
            zero = client.post(action, data=form_for(client.get(ticket), action) | {
                "diagnosis": "0", "labor": "0", "discount": "0"})
            self.assertEqual(zero.status_code, 303)
            form = panel(client.get(ticket), "payment-total")
            self.assertRegex(form, r'name="diagnosis"[^>]*value="0\.00"')
            self.assertRegex(form, r'name="labor"[^>]*value="0\.00"')
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT diagnosis_charge_id, labor_charge_id FROM repair_fees "
                                            "WHERE repair_id = ?", (repair["id"],)).fetchone(), (None, None))

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
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO repair_fees(shop_id, repair_id, diagnosis_charge_id) VALUES (1, 1, 7)")

    def test_upgrade_moves_bookkeeping_rows_into_repair_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repairlib.sqlite3"
            # 0037 converts the old audit bookkeeping; 0038 rebuilds the table with both links.
            initialization.initialize_database(path, "Shop", "UTC", "owner", "hash", target=36)
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


    def test_discount_above_the_subtotal_is_refused_by_the_form_and_the_cli(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            repair = client.post(API, json=DEVICE, headers=auth | {"Idempotency-Key": uuid4().hex}).json()
            ticket = f"/repairs/{repair['id']}"
            action = ticket + "/fees"
            charge = client.post(f"{API}/{repair['id']}/charges",
                                 json={"kind": "labor", "description": "Work", "quantity_milli": 1000,
                                       "unit_price_cents": 1000},
                                 headers=auth | {"Idempotency-Key": uuid4().hex})
            self.assertEqual(charge.status_code, 201, charge.text)
            before = (self.snapshot("SELECT count(*) FROM repair_charges"),
                      self.snapshot("SELECT count(*) FROM repair_discounts"),
                      self.snapshot("SELECT revision FROM repairs WHERE id = ?", (repair["id"],)))
            # 99.00 against a 10.00 subtotal: the domain must refuse with a typed error, not a bare
            # ValueError, so the form shows a refusal page and the CLI prints its message.
            refused = client.post(action, data=form_for(client.get(ticket), action) | {
                "diagnosis": "0", "labor": "0", "discount": "99.00"})
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertIn("cannot exceed the subtotal", refused.text)
            self.assertEqual((self.snapshot("SELECT count(*) FROM repair_charges"),
                              self.snapshot("SELECT count(*) FROM repair_discounts"),
                              self.snapshot("SELECT revision FROM repairs WHERE id = ?", (repair["id"],))),
                             before)
            result = subprocess.run([*COMMAND, "repairs", "fees", str(repair["id"]),
                                     "--workspace", str(self.workspace), "--revision", "1",
                                     "--key", "fee-refusal", "--discount", "99.00"],
                                    text=True, capture_output=True, timeout=20, env=python_env())
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("cannot exceed the subtotal", result.stderr)
            self.assertNotIn("workspace", result.stderr.lower())

if __name__ == "__main__":
    unittest.main()
