"""Billing regressions from the audit: R06 discounts, R07 total, R08 replay, R09–R11 fee rules."""

from contextlib import closing
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from repairlib.client import local
from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import TokenFixture
from test_web_feedback import form_for
from test_web_repair_workspace import panel

FEE_EVENTS = "SELECT count(*) FROM audit_events WHERE operation = 'repair.fees'"


class FeeAuditTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = local.open_workspace(self.workspace)
        self.repair = local.create_repair(self.ctx, DEVICE, "repair").id
        self.counter = 0

    def key(self):
        self.counter += 1
        return f"k{self.counter}"

    def revision(self):
        return local.show_repair(self.ctx, self.repair).revision

    def fees(self, **details):
        return local.set_fees(self.ctx, self.repair, details, self.revision(), self.key())

    def percent(self, value):
        summary = local.charge_summary(self.ctx, self.repair)
        return local.set_discount(self.ctx, self.repair, {
            "kind": "percent", "value": value, "revision": summary.discount_revision}, self.key())

    def rows(self, sql):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql).fetchall()

    def total(self):
        return local.charge_summary(self.ctx, self.repair).total_cents

    # R06
    def test_fee_only_form_keeps_a_percentage_discount(self):
        self.fees(diagnosis=10000)
        self.percent(10)
        with self.client() as client:
            self.sign_in(client)
            ticket = f"/repairs/{self.repair}"
            page = client.get(ticket)
            form = panel(page, "payment-total")
            self.assertIn("10 %", form)
            self.assertNotIn('name="discount"', form)
            sent = client.post(ticket + "/fees", data=form_for(page, ticket + "/fees") | {
                "diagnosis": "100.00", "labor": "5.00"})
            self.assertEqual(sent.status_code, 303, sent.text)
        summary = local.charge_summary(self.ctx, self.repair)
        self.assertEqual((summary.discount_kind, summary.discount_value, summary.total_cents), ("percent", 10, 9450))

    def test_form_loaded_before_a_discount_change_is_refused(self):
        self.fees(diagnosis=10000)
        with self.client() as client:
            self.sign_in(client)
            ticket = f"/repairs/{self.repair}"
            page = client.get(ticket)
            self.percent(10)
            before = (self.total(), self.rows(FEE_EVENTS))
            sent = client.post(ticket + "/fees", data=form_for(page, ticket + "/fees") | {
                "diagnosis": "100.00", "labor": "0", "discount": "0.00"})
            self.assertEqual(sent.status_code, 409, sent.text)
            self.assertEqual((self.total(), self.rows(FEE_EVENTS)), before)
        self.assertEqual(local.charge_summary(self.ctx, self.repair).discount_kind, "percent")

    # R07
    def test_total_is_the_booked_total_and_a_preview_is_labelled(self):
        with self.client() as client:
            self.sign_in(client)
            ticket = f"/repairs/{self.repair}"
            form = panel(client.get(ticket), "payment-total")
            self.assertRegex(form, r"Total: 0\.00")
            self.assertIn("Total if saved", form)
            page = client.get(ticket)
            client.post(ticket + "/fees", data=form_for(page, ticket + "/fees") | {
                "diagnosis": "20.00", "labor": "0", "discount": "0"})
            form = panel(client.get(ticket), "payment-total")
            self.assertRegex(form, r"Total: 20\.00")
            self.assertNotIn("Total if saved", form)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(id, shop_id, username, role, password_hash) "
                       "SELECT (SELECT max(id) + 1 FROM users), 1, 'reader', 'viewer', password_hash FROM users WHERE username = 'owner'")
        other = local.create_repair(self.ctx, DEVICE, "other").id
        with self.client() as viewer:
            self.sign_in(viewer, "reader")
            read_only = panel(viewer.get(f"/repairs/{other}"), "payment-total")
            self.assertRegex(read_only, r"Total: 0\.00")

    # R08
    def test_fee_replay_returns_the_original_summary(self):
        revision, key = self.revision(), "first"
        original = local.set_fees(self.ctx, self.repair, {"diagnosis": 10000}, revision, key)
        self.fees(diagnosis=12000)
        replayed = local.set_fees(self.ctx, self.repair, {"diagnosis": 10000}, revision, key)
        self.assertEqual(replayed, original)
        self.assertEqual(replayed.total_cents, 10000)

    def test_replayed_fee_and_discount_output_does_not_describe_a_later_bill(self):
        def cli(*args):
            done = subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace)],
                                  text=True, capture_output=True, timeout=30, env=python_env())
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

        first = cli("fees", str(self.repair), "--revision", "1", "--key", "cli-fee", "--diagnosis", "100",
                    "--repair", "20")
        cli("fees", str(self.repair), "--revision", "2", "--key", "cli-later", "--diagnosis", "150")
        self.assertEqual(cli("fees", str(self.repair), "--revision", "1", "--key", "cli-fee", "--diagnosis", "100",
                             "--repair", "20"), first)
        discount = cli("discount", str(self.repair), "--revision", "0", "--kind", "amount", "--value", "45",
                       "--key", "cli-discount")
        cli("fees", str(self.repair), "--revision", "3", "--key", "cli-later-2", "--diagnosis", "200")
        self.assertEqual(cli("discount", str(self.repair), "--revision", "0", "--kind", "amount", "--value", "45",
                             "--key", "cli-discount"), discount)

    # R09
    def test_combined_reduction_of_fee_and_discount_is_accepted_as_one_state(self):
        self.fees(labor=10000, discount=9000)
        self.fees(labor=5000, discount=2000)
        self.assertEqual(self.total(), 3000)

    def test_excessive_final_discount_leaves_every_row_unchanged(self):
        self.fees(labor=10000, discount=9000)
        before = (local.charge_summary(self.ctx, self.repair), self.revision(), self.rows(FEE_EVENTS))
        with self.assertRaises(local.ClientError) as caught:
            self.fees(labor=1000, discount=5000)
        self.assertEqual(caught.exception.code, "discount_exceeds_subtotal")
        self.assertEqual((local.charge_summary(self.ctx, self.repair), self.revision(), self.rows(FEE_EVENTS)),
                         before)

    # R10
    def test_generic_edits_cannot_change_a_protected_fee_line(self):
        self.fees(diagnosis=10000)
        line = next(item for item in local.charge_summary(self.ctx, self.repair).items)
        patch = dict(kind="part", description=line.description, unit=line.unit, quantity_milli=1000,
                     unit_price_cents=10000, revision=line.revision, reason="Fix")
        for call in (lambda: local.correct_charge(self.ctx, self.repair, line.id, patch, self.key()),
                     lambda: local.change_charge_visibility(self.ctx, self.repair, line.id, {
                         "revision": line.revision, "deleted": True, "reason": "Remove"}, self.key())):
            with self.assertRaises(local.ClientError) as caught:
                call()
            self.assertEqual(caught.exception.code, "protected_fee")
        after = local.charge_summary(self.ctx, self.repair)
        self.assertEqual((after.parts_subtotal_cents, after.labor_subtotal_cents), (0, 10000))
        free = local.create_charge(self.ctx, self.repair, {
            "kind": "labor", "description": "Extra", "quantity_milli": 1000, "unit_price_cents": 500}, self.key())
        fixed = local.correct_charge(self.ctx, self.repair, free.id, {
            "kind": "labor", "description": "Extra", "unit": free.unit, "quantity_milli": 2000,
            "unit_price_cents": 500, "revision": free.revision, "reason": "Two hours"}, self.key())
        self.assertEqual(fixed.quantity_milli, 2000)

    # R11
    def test_labour_only_save_keeps_the_unsaved_diagnosis_default(self):
        self.fees(labor=3000)
        self.assertEqual(local.ticket_fees(self.ctx, self.repair)["diagnosis_input"], 2000)
        self.fees(diagnosis=0)
        self.assertEqual(local.ticket_fees(self.ctx, self.repair)["diagnosis_input"], 0)

    def test_saving_a_zero_discount_is_not_a_change(self):
        before = (self.revision(), local.charge_summary(self.ctx, self.repair).discount_revision)
        self.fees(discount=0)
        self.fees(discount=0)
        self.assertEqual((self.revision(), local.charge_summary(self.ctx, self.repair).discount_revision), before)

    def test_upgrade_keeps_existing_fee_rows_as_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repairlib.sqlite3"
            initialization.initialize_database(path, "Shop", "UTC", "owner", "hash", target=42)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (1, 1, 'A')")
                db.execute("INSERT INTO repair_fees(shop_id, repair_id) VALUES (1, 1)")
            initialization.upgrade_database(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT diagnosis_saved FROM repair_fees").fetchall(), [(1,)])


if __name__ == "__main__":
    unittest.main()
