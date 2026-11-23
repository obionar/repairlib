"""S4 Q1 gates for independent billing and physical stock corrections."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import subprocess
from threading import Barrier
import unittest
from uuid import uuid4

import httpx

from repairlib.contracts import (
    ChargePatch, ChargeVisibility, DiscountChange, IntakeMistake, ItemInput, PartReturn, RepairFlagChange, RepairInput,
)
from repairlib.domain import charges, inventory, repairs, stock
from repairlib.lifecycle.workspace import database_path
from repairlib.storage import initialization
from test_baseline import python_env
from test_charges import form_fields
from test_cli_identity import COMMAND, workspace_client
from test_inventory_adapters import API as INVENTORY_API, ITEM
import test_repair_consumption as fixture
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret


class ReturnTests(unittest.TestCase):
    token = fixture.ConsumptionTests.token
    snapshot = fixture.ConsumptionTests.snapshot
    refused = fixture.ConsumptionTests.refused
    use = fixture.ConsumptionTests.use

    def setUp(self):
        fixture.ConsumptionTests.setUp(self)
        self.charge = self.use(self.tech, self.first)

    def state(self):
        return fixture.ConsumptionTests.state(self) + [
            self.snapshot("SELECT * FROM charge_requests ORDER BY rowid")]

    def physical(self, key="return", access=None, repair=None, charge_id=None, **changes):
        payload = dict(repair_revision=1, item_revision=3, quantity_milli=1000, reason="Unused fuse")
        return stock.return_part(self.workspace, access or self.tech, (repair or self.first).id,
                                 charge_id or self.charge.id, PartReturn(**(payload | changes)), key=key)

    def combined(self, key="mistake", access=None, repair=None, charge_id=None, **changes):
        payload = dict(repair_revision=1, item_revision=3, charge_revision=1, reason="Wrong intake")
        return stock.correct_intake_mistake(self.workspace, access or self.tech, (repair or self.first).id,
                                           charge_id or self.charge.id, IntakeMistake(**(payload | changes)), key=key)

    def visibility(self, revision, deleted, access=None, key=None):
        return charges.change_charge_visibility(
            self.workspace, access or self.tech, self.first.id, self.charge.id,
            ChargeVisibility(revision, deleted, "Billing correction"), key=key or f"visibility-{revision}")

    def correction(self, key="billing", **changes):
        current = self.charge
        patch = dict(kind=current.kind, description=current.description, unit=current.unit,
                     unit_cost_cents=current.unit_cost_cents, quantity_milli=500, unit_price_cents=0,
                     revision=1, reason="Supplied free")
        return charges.correct_charge(self.workspace, self.tech, self.first.id, current.id,
                                      ChargePatch(**(patch | changes)), key=key)

    def test_physical_return_credits_the_bill_at_the_charge_price(self):
        first = self.physical()
        self.assertEqual(first, self.physical())
        self.assertEqual((first.charge.quantity_milli, first.charge.unit_price_cents, first.charge.revision,
                          first.charge.deleted, first.totals.total_cents), (500, 1999, 2, False, 1000))
        rest = self.physical("rest", item_revision=4, quantity_milli=1000)
        self.assertEqual((rest.charge.quantity_milli, rest.charge.deleted, rest.totals.total_cents,
                          rest.remaining_quantity_milli), (500, True, 0, 2000))
        self.assertEqual(self.snapshot(
            "SELECT json_extract(changes, '$.billed_quantity_milli.after'), json_extract(changes, '$.total_cents.after') "
            "FROM audit_events WHERE operation='inventory.return' ORDER BY id"), [(500, 1000), (0, 0)])

    def test_billing_only_free_remove_restore_and_physical_return(self):
        before = self.state()
        self.refused("inventory_linked", lambda: self.correction(description="Rewrite snapshot"))
        self.assertEqual(self.state(), before)
        free = self.correction()
        self.assertEqual(free, self.correction())
        self.refused("stale_revision", lambda: self.correction(key="stale"))
        self.assertEqual((free.quantity_milli, free.line_total_cents, free.consumed_quantity_milli,
                          free.unit, free.unit_cost_cents, free.movement_id),
                         (500, 0, 4000, self.charge.unit, self.charge.unit_cost_cents, self.charge.movement_id))
        removed = self.visibility(2, True)
        self.assertTrue(removed.deleted)
        self.assertEqual(self.visibility(2, True), removed)
        self.assertEqual(self.state()[1:4], before[1:4])
        before = self.state()
        self.refused("same_state", lambda: self.combined(charge_revision=3))
        self.assertEqual(self.state(), before)
        physical = self.physical()
        self.assertEqual(physical, self.physical())
        self.assertEqual((physical.stock.movement.kind, physical.stock.movement.reverses_id,
                          physical.stock.item.on_hand_milli, physical.remaining_quantity_milli),
                         ("return", self.charge.movement_id, 2000, 3000))
        self.assertEqual(physical.charge, removed)
        stock_after_return = self.state()[1:4]
        restored = self.visibility(3, False)
        self.assertFalse(restored.deleted)
        self.assertEqual(self.visibility(3, False), restored)
        self.assertEqual(self.state()[1:4], stock_after_return)
        physical2 = self.physical("part-two", item_revision=4, quantity_milli=1000)
        self.assertEqual((physical2.remaining_quantity_milli, physical2.charge.deleted), (2000, True))
        before = self.state()
        for movement_id in (self.charge.movement_id, physical.stock.movement.id):
            self.refused("not_reversible", lambda: stock.reverse_movement(
                self.workspace, self.tech, self.item.id, movement_id, "Generic", revision=5, key="generic"))
        self.refused("return_exceeds_consumption", lambda: self.physical(
            "over", item_revision=5, quantity_milli=2001))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.snapshot(
            "SELECT operation, actor_user_id, source, json_extract(changes, '$.reason') "
            "FROM audit_events WHERE operation LIKE 'charge.%' ORDER BY id"),
            [("charge.correct", 2, "api", "Supplied free"), ("charge.delete", 2, "api", "Billing correction"),
             ("charge.restore", 2, "api", "Billing correction")])

    def test_combined_rolls_back_every_write_and_reuses_totals_validation(self):
        for verb, table in (("UPDATE", "inventory_items"), ("INSERT", "inventory_movements"),
                            ("UPDATE", "repair_charges"), ("INSERT", "stock_requests"),
                            ("INSERT", "audit_events")):
            with self.subTest(table=table):
                before = self.state()
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(f"CREATE TRIGGER fail_return BEFORE {verb} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
                try:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic write failure"):
                        self.combined()
                    self.assertEqual(self.state(), before)
                finally:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("DROP TRIGGER fail_return")

        charges.set_discount(self.workspace, self.tech, self.first.id,
                             DiscountChange(0, "amount", 100), key="discount")
        before = self.state()
        self.refused("discount_exceeds_subtotal", self.combined)
        self.assertEqual(self.state(), before)
        partial = self.physical()  # 1.5 billed - 1 returned = 0.5 x 19.99, less the 1.00 discount
        self.assertEqual(partial.totals.total_cents, 900)
        charges.set_discount(self.workspace, self.tech, self.first.id,
                             DiscountChange(1, "none", 0), key="clear")
        done = self.combined(item_revision=4, charge_revision=2)
        self.assertEqual(done, self.combined(item_revision=4, charge_revision=2))
        self.assertEqual((done.stock.movement.quantity_milli, done.stock.item.on_hand_milli,
                          done.remaining_quantity_milli, done.totals.total_cents), (3000, 5000, 0, 0))
        self.assertTrue(done.charge.deleted)
        self.assertEqual(self.snapshot(
            "SELECT actor_user_id, source, token_label, json_extract(changes, '$.reason'), "
            "json_extract(changes, '$.total_cents.after') FROM audit_events WHERE operation='inventory.intake_mistake'"),
            [(2, "api", "Synthetic stock", "Wrong intake", 0)])
        before = self.state()
        self.refused("return_exceeds_consumption", lambda: self.physical("later", item_revision=5))
        self.assertEqual(self.state(), before)
        self.assertEqual(stock.reconcile_stock(self.workspace, self.viewer).mismatches, [])

    def test_racing_returns_and_consumption_with_reviewed_retries(self):
        barrier = Barrier(3)

        def physical(key):
            return self.physical(key, quantity_milli=3000)

        def run(action):
            barrier.wait(timeout=10)
            try:
                return action()
            except stock.StockError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run, action) for action in (
                lambda: physical("r1"), lambda: physical("r2"),
                lambda: self.use(self.owner, self.second, "racing-use", item_revision=3,
                                 consumed_quantity_milli=1000))]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results.count("stale_revision"), 2)
        if results[2] != "stale_revision":
            self.physical("reviewed-return", item_revision=4, quantity_milli=3000)
        else:
            self.use(self.owner, self.second, "racing-use", item_revision=4, consumed_quantity_milli=1000)
        before = self.state()
        self.refused("return_exceeds_consumption", lambda: self.physical(
            "reviewed-over", item_revision=5, quantity_milli=1001))
        self.assertEqual(self.state(), before)
        self.assertEqual(inventory.read_item(self.workspace, self.tech, self.item.id).on_hand_milli, 3000)

    def test_shop_role_and_archive_refusals_leave_no_effects(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            foreign_id = db.execute("INSERT INTO repairs(shop_id, device_type, reported_issue) "
                                    "VALUES (2, 'Foreign', 'Synthetic')").lastrowid
        foreign = replace(self.first, id=foreign_id)
        before = self.state()
        for action in (self.physical, self.combined):
            for code, args in (("forbidden", {"access": self.viewer}),
                               ("unauthenticated", {"access": self.other}),
                               ("not_found", {"repair": foreign}), ("not_found", {"repair": self.second}),
                               ("stale_revision", {"item_revision": 2})):
                self.refused(code, lambda: action(**args))
        self.refused("stale_revision", lambda: self.combined(charge_revision=2))
        self.refused("forbidden", lambda: self.visibility(1, True, self.viewer))
        self.refused("unauthenticated", lambda: self.visibility(1, True, self.other))
        self.assertEqual(self.state(), before)
        inventory.archive_item(self.workspace, self.owner, self.item.id, revision=3, key="archive")
        before = self.state()
        for action in (self.physical, self.combined):
            self.refused("archived", lambda: action(item_revision=4))
        self.assertEqual(self.state(), before)
        self.assertTrue(self.visibility(1, True).deleted)  # Billing is independent of item state.
        repairs.change_repair_flag(self.workspace, self.owner, self.first.id,
                                   RepairFlagChange(revision=1, enabled=True), flag="archive", key="archive")
        before = self.state()
        for action in (self.physical, self.combined):
            self.refused("archived", lambda: action(repair_revision=2, item_revision=4))
        self.refused("archived", lambda: self.visibility(2, False))
        self.assertEqual(self.state(), before)

    def test_populated_schema_19_preserves_links_generic_reversals_and_receipts(self):
        self.workspace = self.workspace.parent / "schema-19"
        self.path = database_path(self.workspace)
        self.path.parent.mkdir(parents=True)
        initialization.initialize_database(self.path, "Prior", "UTC", "owner", "unused", target=19)
        # See the note in test_repair_consumption: seeding runs today's code, so the file is
        # upgraded before it is populated.
        initialization.upgrade_database(self.path)
        self.owner = self.tech = self.token(1)
        self.item = inventory.create_item(self.workspace, self.owner,
                                          ItemInput(sku="OLD", description="Prior", unit="pcs"), key="item")
        self.first = repairs.create_repair(self.workspace, self.owner, RepairInput(**DEVICE), key="repair")
        stock.receive_stock(self.workspace, self.owner, self.item.id, 5000, revision=1, key="receipt")
        self.charge = self.use(self.owner, self.first)
        adjusted = stock.adjust_stock(self.workspace, self.owner, self.item.id, 100, "Count",
                                      revision=3, key="adjust")
        reversed_change = stock.reverse_movement(self.workspace, self.owner, self.item.id, adjusted.movement.id,
                                                 "Fix count", revision=4, key="reverse")
        before = self.state()
        initialization.upgrade_database(self.path)
        initialization.upgrade_database(self.path)
        self.assertEqual(self.snapshot("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.use(self.owner, self.first), self.charge)
        self.assertEqual(stock.reverse_movement(self.workspace, self.owner, self.item.id, adjusted.movement.id,
                                                "Fix count", revision=4, key="reverse"), reversed_change)
        self.assertEqual(self.physical(item_revision=5).remaining_quantity_milli, 3000)


class ReturnAdapterTests(TokenFixture, unittest.TestCase):
    def test_web_billing_http_cli_return_and_lost_combined_response_survive_restart(self):

        def cli(*args, stdin=None, code=0):
            result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                    text=True, capture_output=True, timeout=10, env=python_env())
            self.assertTrue(token not in result.stdout + result.stderr)
            self.assertEqual(result.returncode, code, result.stderr)
            return json.loads(result.stdout) if "--json" in args and code == 0 else result.stdout

        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}

            def send(url, body, key=None):
                return client.post(url, headers=auth | {"Idempotency-Key": key or uuid4().hex}, json=body)

            repair_id = send(API, DEVICE).json()["id"]
            item_id = send(INVENTORY_API, ITEM).json()["id"]
            self.assertEqual(send(f"{INVENTORY_API}/{item_id}/receive", {
                "revision": 1, "quantity_milli": 5000}).status_code, 201)
            charge_id = send(f"{API}/{repair_id}/consume-part", {
                "item_id": item_id, "repair_revision": 1, "item_revision": 2,
                "consumed_quantity_milli": 3000, "billed_quantity_milli": 1500,
                "unit_price_cents": 1999}).json()["id"]
            base = f"{API}/{repair_id}/charges/{charge_id}"
            edit = f"/repairs/{repair_id}/charges/{charge_id}/edit"
            visibility = f"/repairs/{repair_id}/charges/{charge_id}/visibility"
            form = form_fields(client.get(edit), edit) | {
                "description": ITEM["description"], "quantity": "1.5", "unit": ITEM["unit"],
                "unit_price": "0.00", "unit_cost": "0.17", "reason": "Free",
            }
            self.assertEqual(client.post(edit, data=form).status_code, 303)
            self.assertEqual(client.post(edit, data=form).status_code, 303)
            form = form_fields(client.get(edit), visibility) | {"reason": "Free"}
            self.assertEqual(client.post(visibility, data=form).status_code, 303)
            cli("repairs", "restore-charge", str(repair_id), str(charge_id), "--revision", "3",
                "--reason", "Billing restored", "--key", "restore", "--json")
            edit_command = ("repairs", "edit-charge", str(repair_id), str(charge_id), "--revision", "4",
                            "--reason", "Correct billed quantity", "--quantity", "1", "--key", "cli-edit", "--json")
            edited = cli(*edit_command)
            self.assertEqual(edited["quantity_milli"], 1000)
            self.assertEqual(edited, cli(*edit_command))
            status = cli("repairs", "part-return-status", str(repair_id), str(charge_id), "--json")
            self.assertEqual(status, client.get(f"{base}/return-part", headers=auth).json())
            self.assertEqual((status["remaining_quantity_milli"], status["item_revision"], status["charge_revision"]),
                             (3000, 3, 5))
            web_return = f"/repairs/{repair_id}/charges/{charge_id}/return-part"
            web_form = form_fields(client.get(web_return), web_return)
            self.assertEqual(client.post(web_return, data=web_form | {
                "csrf": "invalid", "quantity": "1", "reason": "Spare"}).status_code, 403)
            physical = ("repairs", "return-part", str(repair_id), str(charge_id), "--repair-revision", "1",
                        "--item-revision", "3", "--quantity", "0.5", "--reason", "Spare", "--key", "physical", "--json")
            result = cli(*physical)
            self.assertEqual(result, cli(*physical))
            self.assertEqual((result["stock"]["item"]["on_hand_milli"], result["remaining_quantity_milli"],
                              result["charge"]["quantity_milli"]), (2500, 2500, 500))
            payload = {"repair_revision": 1, "charge_revision": 6, "item_revision": 4, "reason": "Intake error"}
            mistake = f"{base}/intake-mistake"

            def lose_response(response):
                self.assertEqual(response.status_code, 201)
                raise httpx.ReadError("Synthetic lost response")

            client.event_hooks["response"].append(lose_response)
            try:
                with self.assertRaises(httpx.ReadError):
                    send(mistake, payload, "mistake")
            finally:
                client.event_hooks["response"].remove(lose_response)

        with workspace_client(self.workspace) as (client, _):
            # Keys belong to the principal that sent them: the API caller recovers its own
            # lost response, while the CLI actor reusing that key sees the committed change.
            done = send(mistake, payload, "mistake").json()
            self.assertEqual(done, send(mistake, payload, "mistake").json())
            committed = self.snapshot("SELECT * FROM inventory_movements ORDER BY id")
            cli("repairs", "intake-mistake", str(repair_id), str(charge_id), "--repair-revision", "1",
                "--charge-revision", "6", "--item-revision", "4", "--reason", "Intake error",
                "--key", "mistake", "--json", code=7)
            self.assertEqual(self.snapshot("SELECT * FROM inventory_movements ORDER BY id"), committed)
            self.assertEqual(send(mistake, payload | {"reason": "Changed"}, "mistake").status_code, 409)
            self.assertEqual((done["stock"]["item"]["on_hand_milli"], done["totals"]["total_cents"],
                              done["remaining_quantity_milli"]), (5000, 0, 0))
            live = client.get(f"{API}/{repair_id}/charges", params={"include_consumption": "true"},
                              headers=auth).json()
            self.assertEqual(live.pop("items"), [done["charge"]])
            live.pop("diagnosis_cents")  # A summary field, not one of the totals.
            self.assertEqual(live, done["totals"])
            self.assertEqual((done["charge"]["consumed_quantity_milli"], done["charge"]["quantity_milli"],
                              done["charge"]["deleted"]), (3000, 500, True))
            self.assertEqual(self.snapshot(
                "SELECT kind, quantity_milli FROM inventory_movements ORDER BY id"),
                [("receive", 5000), ("consume", -3000), ("return", 500), ("return", 2500)])
