"""Booked amount owed in the existing plain-text repair list."""

from contextlib import closing
import json
import sqlite3
import unittest

from repairlib.client import local
from repairlib.domain import charges
from repairlib.domain.customers import CustomerAccess, CustomerError
from test_agent_surface import cli
from test_repairs import DEVICE
from test_tokens import TokenFixture


class RepairListAmountTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = local.open_workspace(self.workspace)
        self.repair = local.create_repair(self.ctx, DEVICE, "amount-repair")

    def cli(self, *args):
        return cli(self.workspace, *args)

    def add_charge(self, key, repair_id=None, **overrides):
        details = {
            "kind": "part", "description": "Synthetic part", "quantity_milli": 1500,
            "unit": "pcs", "unit_price_cents": 1999, "unit_cost_cents": None, "notes": "",
        }
        return local.create_charge(self.ctx, repair_id or self.repair.id, details | overrides, key)

    def test_list_line_shows_booked_amount_owed_without_widening_json(self):
        self.add_charge("amount-charge")
        before = self.db.read_bytes()

        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        fields = listed.stdout.strip().split("\t")
        self.assertEqual(fields[-1], "owed 29.99 EUR")

        payload = json.loads(self.cli("list", "--json").stdout)
        self.assertNotIn("amount_owed_cents", payload["items"][0])
        self.assertNotIn("owed", payload["items"][0])
        self.assertEqual(self.db.read_bytes(), before)

    def test_invalid_list_page_is_refused_without_writes(self):
        before = self.db.read_bytes()
        refused = self.cli("list", "--limit", "0")
        self.assertEqual(refused.returncode, 8)
        self.assertIn("Invalid repair page.", refused.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_list_amount_matches_discount_and_deleted_charge_summary(self):
        self.add_charge("amount-part")
        self.add_charge("amount-labor", kind="labor", description="Bench diagnosis",
                        quantity_milli=250, unit="h", unit_price_cents=4001)
        removed = self.add_charge("amount-removed", quantity_milli=1000, unit_price_cents=500)
        local.change_charge_visibility(self.ctx, self.repair.id, removed.id,
                                       {"revision": 1, "deleted": True, "reason": "Duplicate"}, "amount-delete")
        local.set_discount(self.ctx, self.repair.id,
                           {"revision": 0, "kind": "percent", "value": 15}, "amount-discount")
        self.assertEqual(local.charge_summary(self.ctx, self.repair.id).total_cents, 3399)

        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.strip().split("\t")[-1], "owed 33.99 EUR")

    def test_paid_rows_owe_zero_but_collected_unpaid_rows_keep_their_total(self):
        self.add_charge("paid-charge", quantity_milli=1000, unit_price_cents=1250)
        staged = local.change_stage(self.ctx, self.repair.id,
                                    {"revision": 1, "stage": "no_repair", "reason": ""}, "paid-stage")
        local.change_repair_flag(self.ctx, self.repair.id, "payment",
                                 {"revision": staged.revision, "enabled": True}, "paid-payment")

        collected = local.create_repair(self.ctx, DEVICE, "collected-repair")
        self.add_charge("collected-charge", collected.id, quantity_milli=1000, unit_price_cents=750)
        staged = local.change_stage(self.ctx, collected.id,
                                    {"revision": 1, "stage": "no_repair", "reason": ""}, "collected-stage")
        local.change_repair_flag(self.ctx, collected.id, "pickup",
                                 {"revision": staged.revision, "enabled": True}, "collected-pickup")

        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = {fields[0]: fields for line in listed.stdout.splitlines() if (fields := line.split("\t"))}
        self.assertEqual(rows[str(self.repair.id)][-1], "paid owed 0.00 EUR")
        self.assertEqual(rows[str(collected.id)][-1], "unpaid collected owed 7.50 EUR")

    def test_amounts_follow_paging_and_keep_each_row_revision(self):
        self.add_charge("page-one", quantity_milli=1000, unit_price_cents=100)
        second = local.create_repair(self.ctx, DEVICE, "page-two-repair")
        third = local.create_repair(self.ctx, DEVICE, "page-three-repair")
        self.add_charge("page-two", second.id, quantity_milli=1000, unit_price_cents=200)
        self.add_charge("page-three", third.id, quantity_milli=1000, unit_price_cents=300)

        first = self.cli("list", "--limit", "2")
        self.assertEqual(first.returncode, 0, first.stderr)
        rows = [line.split("\t") for line in first.stdout.splitlines()]
        self.assertEqual([[fields[0], fields[5], fields[-1]] for fields in rows], [
            [str(third.id), "r1", "owed 3.00 EUR"],
            [str(second.id), "r1", "owed 2.00 EUR"],
        ])
        self.assertIn(f"Next page: --before {second.id}", first.stderr)

        following = self.cli("list", "--limit", "2", "--before", str(second.id))
        self.assertEqual(following.returncode, 0, following.stderr)
        self.assertEqual(following.stdout.strip().split("\t")[-1], "owed 1.00 EUR")

    def test_bulk_totals_keep_shop_isolation_and_authentication(self):
        self.add_charge("scope-charge", quantity_milli=1000, unit_price_cents=100)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside')")
            db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (50, 2, 'Hidden')")
            db.execute("""INSERT INTO users(id, shop_id, username, role, password_hash)
                       SELECT 50, 2, 'outsider', 'admin', password_hash FROM users WHERE id = 1""")
            db.execute("""INSERT INTO repair_charges(
                           id, shop_id, repair_id, kind, description, quantity_milli,
                           unit, unit_price_cents, created_by, created_by_username)
                       VALUES (50, 2, 50, 'part', 'Hidden', 1000, 'pcs', 99900, 50, 'outsider')""")

        totals = local.repair_booked_totals(self.ctx, (self.repair.id, 50))
        self.assertEqual(totals, {self.repair.id: 100, 50: 0})
        before = self.db.read_bytes()
        with self.assertRaises(CustomerError) as caught:
            charges.booked_totals(self.workspace, CustomerAccess("api", ""), (self.repair.id,))
        self.assertEqual((caught.exception.status, caught.exception.code), (401, "unauthenticated"))
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
