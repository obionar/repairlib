"""S3 free-text parts/labor charges, discount and deterministic totals."""

from contextlib import closing
import json
import re
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from argon2 import PasswordHasher

from repairlib import money
from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import PASSWORD, TokenFixture
from test_users import ACCOUNT, hidden, issued_secret

PART = {"kind": "part", "description": "Synthetic charging port", "quantity_milli": 1500,
        "unit": "pcs", "unit_price_cents": 1999, "unit_cost_cents": 800, "notes": ""}
LABOR = {"kind": "labor", "description": "Bench diagnosis", "quantity_milli": 250,
         "unit": "h", "unit_price_cents": 4001}


def form_fields(page, action):
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', page.text, re.S)
    if not match:
        raise AssertionError(f"Missing form for {action}")
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match[1]))


class MoneyTests(unittest.TestCase):
    def test_parsing_rounding_discounts_and_bounds_without_floats(self):
        self.assertEqual(money.parse_cents("12"), 1200)
        self.assertEqual(money.parse_cents(" 12.3 "), 1230)
        self.assertEqual(money.parse_cents("0.05"), 5)
        self.assertEqual(money.parse_milli("1.5"), 1500)
        self.assertEqual(money.parse_milli("0.001"), 1)
        for bad in ("", "-1", "+1", "1e3", "1.234", "1,5", ".5", "5.", "١٢", "nan", "1_000"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_cents(bad)
        for bad in ("0", "0.000", "1.2345", "1000000.001"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_milli(bad)
        with self.assertRaises(ValueError):
            money.parse_cents("100000000.01")
        self.assertEqual(money.line_total(1500, 1999), 2999)  # 2998.5 rounds half-up
        self.assertEqual(money.line_total(250, 4001), 1000)   # 1000.25 rounds down
        self.assertEqual(money.line_total(1, 500), 1)          # 0.5 rounds up
        self.assertEqual(money.discount("percent", 15, 3999), 600)  # 599.85
        self.assertEqual(money.discount("percent", 50, 1), 1)       # 0.5 rounds up
        self.assertEqual(money.discount("amount", 500, 3999), 500)
        self.assertEqual(money.discount("none", 0, 3999), 0)
        with self.assertRaises(ValueError):
            money.discount("amount", 4000, 3999)
        with self.assertRaises(ValueError):
            money.discount("percent", 101, 3999)
        self.assertEqual(money.format_cents(123456), "1234.56")
        self.assertEqual(money.format_milli(1500), "1.5")
        self.assertEqual(money.format_milli(250), "0.25")
        self.assertEqual(money.format_milli(2000), "2")


class ChargeTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def add(self, client, auth, repair_id, payload, key=None):
        return client.post(f"{API}/{repair_id}/charges", json=payload,
                           headers={**auth, "Idempotency-Key": key or uuid4().hex})

    def test_api_totals_replay_corrections_discount_and_restart(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            key = uuid4().hex
            part = self.add(client, auth, repair["id"], PART, key)
            self.assertEqual(part.status_code, 201, part.text)
            self.assertEqual((part.json()["line_total_cents"], part.json()["created_by_username"]),
                             (2999, "owner"))
            self.assertEqual(self.add(client, auth, repair["id"], PART, key).json(), part.json())
            self.assertEqual(self.add(client, auth, repair["id"], {**PART, "quantity_milli": 1}, key)
                             .status_code, 409)
            self.assertEqual(self.add(client, auth, repair["id"], LABOR).status_code, 201)
            for bad in ({**PART, "unit_price_cents": -1}, {**PART, "quantity_milli": 0},
                        {**PART, "description": " "}, {**PART, "unit_price_cents": 10**10 + 1},
                        {**PART, "unit_price_cents": 1.5}, {**PART, "kind": "fee"}):
                self.assertEqual(self.add(client, auth, repair["id"], bad).status_code, 422, bad)
            summary = client.get(f"{API}/{repair['id']}/charges", headers=auth).json()
            self.assertEqual((summary["parts_subtotal_cents"], summary["labor_subtotal_cents"],
                              summary["subtotal_cents"], summary["discount_cents"],
                              summary["total_cents"], summary["currency"]),
                             (2999, 1000, 3999, 0, 3999, "EUR"))

            discount_url = f"{API}/{repair['id']}/discount"
            excessive = client.put(discount_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 0, "kind": "amount", "value": 4000})
            self.assertEqual(excessive.status_code, 422)
            discount = client.put(discount_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 0, "kind": "percent", "value": 15})
            self.assertEqual(discount.status_code, 200, discount.text)
            self.assertEqual((discount.json()["discount_cents"], discount.json()["total_cents"],
                              discount.json()["discount_revision"]), (600, 3399, 1))
            stale = client.put(discount_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"revision": 0, "kind": "none", "value": 0})
            self.assertEqual(stale.status_code, 409)
            amount = client.put(discount_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"revision": 1, "kind": "amount", "value": 3000})
            self.assertEqual(amount.json()["total_cents"], 999)

            url = f"{API}/{repair['id']}/charges/{part.json()['id']}"
            no_reason = client.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                     json={**PART, "revision": 1, "unit_price_cents": 2500, "reason": ""})
            self.assertEqual(no_reason.status_code, 422)
            corrected = client.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                     json={**PART, "revision": 1, "unit_price_cents": 2500,
                                           "reason": "Supplier price corrected"})
            self.assertEqual(corrected.status_code, 200, corrected.text)
            self.assertEqual((corrected.json()["revision"], corrected.json()["line_total_cents"]), (2, 3750))
            blocked = client.post(f"{url}/visibility", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 2, "deleted": True, "reason": "Duplicate"})
            self.assertEqual(blocked.status_code, 409)  # 30.00 discount would exceed 10.00 subtotal
            client.put(discount_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                       json={"revision": 2, "kind": "none", "value": 0})
            deleted = client.post(f"{url}/visibility", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 2, "deleted": True, "reason": "Duplicate"})
            self.assertEqual((deleted.status_code, deleted.json()["deleted"]), (200, True))
            denied = client.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={**PART, "revision": 3, "reason": "Deleted lines cannot change"})
            self.assertEqual(denied.status_code, 409)
            restored = client.post(f"{url}/visibility", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 3, "deleted": False, "reason": "Not a duplicate"})
            self.assertEqual(restored.json()["revision"], 4)

        with self.client() as restarted:
            _, auth = self.admin_auth(restarted)
            summary = restarted.get(f"{API}/{repair['id']}/charges", headers=auth).json()
            self.assertEqual((summary["subtotal_cents"], summary["total_cents"], len(summary["items"])),
                             (4750, 4750, 2))
        operations = [row[0] for row in self.snapshot(
            "SELECT operation FROM audit_events WHERE entity_type IN ('charge','repair_discount') ORDER BY id")]
        self.assertEqual(operations, ["charge.create", "charge.create", "repair.discount", "repair.discount",
                                      "charge.correct", "repair.discount", "charge.delete", "charge.restore"])
        correction = json.loads(self.snapshot(
            "SELECT changes FROM audit_events WHERE operation='charge.correct'")[0][0])
        self.assertEqual(correction["unit_price_cents"], {"before": 1999, "after": 2500})
        self.assertEqual(correction["reason"], "Supplier price corrected")

    def test_viewer_cost_hidden_second_shop_archive_and_line_limit_refusals(self):
        with self.client() as owner, self.client() as viewer:
            _, auth = self.admin_auth(owner)
            repair = self.repair(owner, auth)
            self.assertEqual(self.add(owner, auth, repair["id"], PART).status_code, 201)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
                db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                           "VALUES (50,2,'outsider','admin','unused')")
                db.execute("INSERT INTO repair_charges(id,shop_id,repair_id,kind,description,quantity_milli,"
                           "unit,unit_price_cents,unit_cost_cents,created_by,created_by_username) VALUES "
                           "(50,2,50,'part','Hidden part',1000,'pcs',100,50,50,'outsider')")
            self.assertEqual(owner.get(f"{API}/50/charges", headers=auth).status_code, 404)
            self.assertEqual(self.add(owner, auth, 50, PART).status_code, 404)
            self.assertEqual(owner.patch(f"{API}/{repair['id']}/charges/50",
                                         headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={**PART, "revision": 1, "reason": "x"}).status_code, 404)
            self.assertNotIn("Hidden part", owner.get(f"{API}/{repair['id']}/charges", headers=auth).text)

            self.sign_in(viewer, "viewer")
            viewer_token = issued_secret(viewer.post(ACCOUNT, data={
                "csrf": hidden(viewer.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Viewer read", "scope": "read", "password": PASSWORD,
            }))
            viewer_auth = {"Authorization": f"Bearer {viewer_token}"}
            seen = viewer.get(f"{API}/{repair['id']}/charges", headers=viewer_auth)
            self.assertEqual(seen.status_code, 200)
            self.assertIsNone(seen.json()["items"][0]["unit_cost_cents"])
            self.assertEqual(seen.json()["total_cents"], 2999)
            self.assertEqual(self.add(viewer, viewer_auth, repair["id"], PART).status_code, 403)
            page = viewer.get(f"/repairs/{repair['id']}")
            self.assertIn("29.99", page.text)
            self.assertNotIn("8.00", page.text)

            with patch("repairlib.domain.charges.MAX_ACTIVE_CHARGES", 1):
                self.assertEqual(self.add(owner, auth, repair["id"], LABOR).status_code, 409)
            owner.post(f"{API}/{repair['id']}/archive", headers={**auth, "Idempotency-Key": uuid4().hex},
                       json={"revision": 1, "enabled": True})
            self.assertEqual(self.add(owner, auth, repair["id"], LABOR).status_code, 409)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM repair_charges WHERE shop_id=1"), [(1,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE shop_id=2"), [(0,)])

    def test_browser_decimal_charge_correction_and_discount_without_javascript(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            base = f"/repairs/{repair['id']}"
            page = client.get(f"{base}/charges/new")
            self.assertEqual(page.status_code, 200)
            form = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"), "kind": "part",
                    "description": "Synthetic screen", "quantity": "1.5", "unit": "pcs",
                    "unit_price": "19.99", "unit_cost": "8", "notes": ""}
            invalid = client.post(f"{base}/charges", data={**form, "unit_price": "19.999"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('value="19.999"', invalid.text)
            for _ in range(2):
                self.assertEqual(client.post(f"{base}/charges", data=form).status_code, 303)
            summary = client.get(f"{API}/{repair['id']}/charges", headers=auth).json()
            self.assertEqual((len(summary["items"]), summary["total_cents"]), (1, 2999))
            detail = client.get(base)
            self.assertIn("29.99", detail.text)
            charge = summary["items"][0]
            edit_page = client.get(f"{base}/charges/{charge['id']}/edit")
            edited = {**form, "csrf": hidden(edit_page, "csrf"), "key": hidden(edit_page, "key"),
                      "revision": "1", "unit_price": "25", "reason": "Browser price correction"}
            self.assertEqual(client.post(f"{base}/charges/{charge['id']}/edit", data=edited).status_code, 303)
            detail = client.get(base)
            discount = {**form_fields(detail, f"{base}/fees"),
                        "labor": "0", "discount": "3.75"}
            self.assertEqual(client.post(f"{base}/fees", data=discount).status_code, 303)
            detail = client.get(base)
            self.assertIn("37.50", detail.text)
            self.assertIn("33.75", detail.text)  # 37.50 - 3.75

    def test_real_http_cli_charges_discount_and_totals(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair = self.repair(client, auth)

            def cli(*args):
                result = subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            key = uuid4().hex
            added = cli("add-charge", str(repair["id"]), "--kind", "part", "--description", "CLI part",
                        "--quantity", "1.5", "--unit-price", "19.99", "--unit-cost", "8", "--key", key)
            self.assertEqual(added["line_total_cents"], 2999)
            self.assertEqual(cli("add-charge", str(repair["id"]), "--kind", "part", "--description",
                                 "CLI part", "--quantity", "1.5", "--unit-price", "19.99",
                                 "--unit-cost", "8", "--key", key), added)
            cli("add-charge", str(repair["id"]), "--kind", "labor", "--description", "CLI labor",
                "--quantity", "0.25", "--unit", "h", "--unit-price", "40.01", "--key", uuid4().hex)
            corrected = cli("edit-charge", str(repair["id"]), str(added["id"]), "--revision", "1",
                            "--unit-price", "25", "--reason", "CLI correction", "--key", uuid4().hex)
            self.assertEqual(corrected["line_total_cents"], 3750)
            totals = cli("discount", str(repair["id"]), "--revision", "0", "--kind", "amount",
                         "--value", "7.5", "--key", uuid4().hex)
            self.assertEqual((totals["subtotal_cents"], totals["discount_cents"], totals["total_cents"]),
                             (4750, 750, 4000))
            deleted = cli("delete-charge", str(repair["id"]), str(added["id"]), "--revision", "2",
                          "--reason", "CLI removal", "--key", uuid4().hex)
            self.assertTrue(deleted["deleted"])
            summary = cli("charges", str(repair["id"]))
            self.assertEqual(summary["total_cents"], 250)

    def test_populated_schema_ten_upgrade_and_failed_migration_rollback(self):
        restored = self.workspace.parent / "restore"
        (restored / "state").mkdir(parents=True)
        copy = restored / "state" / self.db.name
        initialization.initialize_database(copy, "Previous shop", "UTC", "owner",
                                           PasswordHasher().hash(PASSWORD), target=10)
        with closing(sqlite3.connect(copy)) as db, db:
            db.execute("INSERT INTO repairs(shop_id,reported_issue) VALUES (1,'Prior issue')")
        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic migration failure")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(copy)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (10,))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='repair_charges'").fetchone())
        result = subprocess.run([*COMMAND, "migrate", "--workspace", str(restored)],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT reported_issue FROM repairs").fetchall(), [("Prior issue",)])
            self.assertGreaterEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)


if __name__ == "__main__":
    unittest.main()
