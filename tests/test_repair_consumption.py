"""S4 gate for catalog consumption and its repair charge in one transaction."""

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

from repairlib.contracts import ItemInput, PartConsumption, RepairFlagChange, RepairInput
from repairlib.domain import charges, inventory, repairs, stock
from repairlib.domain.repairs import RepairError
from repairlib.lifecycle.workspace import database_path
from repairlib.storage import initialization
from test_baseline import python_env
from test_charges import form_fields
from test_cli_identity import COMMAND, workspace_client
from test_inventory_adapters import API as INVENTORY_API, ITEM
from test_repairs import API, DEVICE
import test_stock
from test_tokens import ACCOUNT, TokenFixture, issued_secret

TABLES = ("repairs", "inventory_items", "inventory_movements", "stock_requests",
          "repair_charges", "repair_part_consumptions", "audit_events")
# Receipt tables gain a migration-managed age column; the gate compares business columns.
RECEIPT_COLUMNS = {"stock_requests": "shop_id, actor_id, operation, request_key, fingerprint, "
                                    "movement_id, result"}


def rows(test, table):
    return test.snapshot(f"SELECT {RECEIPT_COLUMNS.get(table, '*')} FROM {table} ORDER BY rowid")


class ConsumptionTests(unittest.TestCase):
    token = test_stock.StockTests.token
    snapshot = test_stock.StockTests.snapshot
    refused = test_stock.StockTests.refused

    def setUp(self):
        test_stock.StockTests.setUp(self)
        self.first, self.second = [
            repairs.create_repair(self.workspace, self.owner, RepairInput(**DEVICE), key=f"repair-{i}")
            for i in range(2)
        ]
        stock.receive_stock(self.workspace, self.owner, self.item.id, 5000,
                            revision=1, key="receipt")

    def state(self):
        return [rows(self, table) for table in TABLES]

    def use(self, access, repair, key="use", **changes):
        payload = dict(item_id=self.item.id, repair_revision=repair.revision, item_revision=2,
                       consumed_quantity_milli=4000, billed_quantity_milli=1500, unit_price_cents=1999)
        return stock.consume_for_repair(self.workspace, access, repair.id,
                                        PartConsumption(**(payload | changes)), key=key)

    def test_concurrent_repairs_one_wins_then_reviewed_retry_cannot_oversell(self):
        barrier = Barrier(2)

        def use(access, repair):
            barrier.wait(timeout=10)
            try:
                return self.use(access, repair)
            except RepairError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(use, actor, repair) for actor, repair in
                             ((self.owner, self.first), (self.tech, self.second))]
            results = [future.result(timeout=20) for future in (first, second)]
        self.assertEqual(results.count("stale_revision"), 1)
        loser_index = results.index("stale_revision")
        loser, loser_actor = ((self.first, self.owner), (self.second, self.tech))[loser_index]
        after = self.state()
        self.refused("insufficient_stock", lambda: self.use(loser_actor, loser, item_revision=3))
        self.assertEqual(self.state(), after)
        winner = results[1 - loser_index]
        self.assertEqual(self.snapshot("SELECT id, repair_id FROM repair_charges"),
                         [(winner.id, winner.repair_id)])
        self.assertEqual(self.snapshot("SELECT kind, quantity_milli FROM inventory_movements ORDER BY id"),
                         [("receive", 5000), ("consume", -4000)])
        self.assertEqual(inventory.read_item(self.workspace, self.tech, self.item.id).on_hand_milli, 1000)
        self.assertEqual(stock.reconcile_stock(self.workspace, self.viewer).mismatches, [])

    def test_rollback_retry_and_refusal_matrix(self):
        # Fail at charge insert, durable linkage/history, retry history, and attributed audit.
        for table in ("repair_charges", "repair_part_consumptions", "stock_requests", "audit_events"):
            with self.subTest(table=table):
                before = self.state()
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(f"CREATE TRIGGER fail_use BEFORE INSERT ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
                try:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic write failure"):
                        self.use(self.tech, self.first)
                    self.assertEqual(self.state(), before)
                finally:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("DROP TRIGGER fail_use")
        with closing(sqlite3.connect(self.path)) as db, db:
            outside_item = db.execute("INSERT INTO inventory_items(shop_id, sku, description, unit) "
                                      "VALUES (2, 'FOREIGN', 'Foreign', 'pcs')").lastrowid
            outside_repair = db.execute("INSERT INTO repairs(shop_id, device_type, reported_issue) "
                                        "VALUES (2, 'Outside', 'Synthetic')").lastrowid
        before = self.state()
        for code, access, repair, changes in (
            ("forbidden", self.viewer, self.first, {}),
            ("unauthenticated", self.other, self.first, {}),
            ("not_found", self.owner, self.first, {"item_id": outside_item}),
            ("not_found", self.owner, replace(self.first, id=outside_repair), {}),
            ("stale_revision", self.owner, self.first, {"repair_revision": 9}),
            ("validation", self.owner, self.first, {"consumed_quantity_milli": 1.5}),
            ("validation", self.owner, self.first, {"billed_quantity_milli": 0}),
            ("validation", self.owner, self.first, {"unit_price_cents": -1}),
        ):
            with self.subTest(code=code, changes=changes):
                self.refused(code, lambda: self.use(access, repair, **changes))
        self.assertEqual(self.state(), before)

        charge = self.use(self.tech, self.first)
        self.assertEqual((charge.description, charge.unit, charge.consumed_quantity_milli,
                          charge.quantity_milli, charge.unit_price_cents, charge.line_total_cents,
                          charge.inventory_item_id), ("Fuse", "pcs", 4000, 1500, 1999, 2999, self.item.id))
        before = self.state()
        self.refused("conflict", lambda: self.use(self.tech, self.second))
        self.refused("conflict", lambda: self.use(self.tech, self.first, billed_quantity_milli=1000))
        self.refused("not_found", lambda: charges.read_charge(
            self.workspace, self.tech, self.second.id, charge.id))
        self.refused("not_reversible", lambda: stock.reverse_movement(
            self.workspace, self.tech, self.item.id, charge.movement_id, "Wrong part", revision=3, key="reverse"))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.snapshot(
            "SELECT actor_user_id, source, token_label, json_extract(changes, '$.movement_id') "
            "FROM audit_events WHERE operation='inventory.consume'"),
            [(2, "api", "Synthetic stock", charge.movement_id)])
        inventory.archive_item(self.workspace, self.owner, self.item.id, revision=3, key="archive")
        repairs.change_repair_flag(self.workspace, self.owner, self.second.id,
                                   RepairFlagChange(revision=self.second.revision, enabled=True),
                                   flag="archive", key="archive")
        before = self.state()
        self.refused("archived", lambda: self.use(self.tech, self.first, "archived-item", item_revision=4))
        self.refused("archived", lambda: self.use(self.tech, self.second, "archived-repair",
                                                 repair_revision=2, item_revision=4))
        self.assertEqual(self.use(self.tech, self.first), charge)
        self.assertEqual(self.state(), before)

    def test_populated_schema_18_ledger_reversals_and_receipts_survive_upgrade(self):
        self.workspace = self.workspace.parent / "schema-18"
        self.path = database_path(self.workspace)
        self.path.parent.mkdir(parents=True)
        initialization.initialize_database(self.path, "Prior", "UTC", "owner", "unused", target=18)
        # This test populates the workspace with today's code, which requires the current schema:
        # writing into a schema-18 file is not a supported state (startup and `migrate` upgrade
        # first). The historical-schema upgrade itself is covered by the tests that seed rows with
        # SQL before upgrading.
        initialization.upgrade_database(self.path)
        self.owner = self.token(1)
        self.item = inventory.create_item(self.workspace, self.owner,
                                          ItemInput(sku="OLD", description="Prior fuse", unit="pcs"), key="item")
        repair = repairs.create_repair(self.workspace, self.owner, RepairInput(**DEVICE), key="repair")
        received = stock.receive_stock(self.workspace, self.owner, self.item.id, 5000, revision=1, key="receive")
        adjusted = stock.adjust_stock(self.workspace, self.owner, self.item.id, -1000, "Count",
                                      revision=2, key="adjust")
        reversed_change = stock.reverse_movement(self.workspace, self.owner, self.item.id, adjusted.movement.id,
                                                 "Restock", revision=3, key="reverse")
        tables = ("inventory_items", "inventory_movements", "stock_requests")
        before = [rows(self, table) for table in tables]
        initialization.upgrade_database(self.path)
        self.assertEqual(self.snapshot("PRAGMA foreign_key_check"), [])
        self.assertEqual([rows(self, table) for table in tables], before)
        self.assertEqual(stock.receive_stock(self.workspace, self.owner, self.item.id, 5000,
                                             revision=1, key="receive"), received)
        self.assertEqual(stock.reverse_movement(self.workspace, self.owner, self.item.id, adjusted.movement.id,
                                                "Restock", revision=3, key="reverse"), reversed_change)
        free = self.use(self.owner, repair, item_revision=4, unit_price_cents=0)
        self.assertEqual((free.consumed_quantity_milli, free.line_total_cents), (4000, 0))
        self.assertEqual(stock.reconcile_stock(self.workspace, self.owner).mismatches, [])


class ConsumptionAdapterTests(TokenFixture, unittest.TestCase):
    def test_ticket_part_search_uses_a_catalog_part_in_one_step(self):
        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}

            def send(url, body):
                return client.post(url, headers=auth | {"Idempotency-Key": uuid4().hex}, json=body)

            repair_id = send(API, DEVICE).json()["id"]
            item = send(INVENTORY_API, ITEM).json()
            item_url = f"{INVENTORY_API}/{item['id']}"
            self.assertEqual(send(f"{item_url}/receive", {"revision": 1, "quantity_milli": 3000}).status_code, 201)
            ticket = client.get(f"/repairs/{repair_id}", params={"part_q": "RC-1"})
            self.assertEqual(ticket.status_code, 200)
            parts = ticket.text.split('aria-labelledby="stock-usage"', 1)[1].split('</section>', 1)[0]
            self.assertIn('name="part_q" value="RC-1"', parts)
            action = f"/repairs/{repair_id}/consume-part/{item['id']}"
            self.assertIn("Synthetic &lt;capacitor&gt;", parts)
            fields = form_fields(ticket, action)
            self.assertEqual(set(fields), {"csrf", "key", "repair_revision", "item_revision"})
            before = client.get(item_url, headers=auth).json()["on_hand_milli"]
            saved = client.post(action, data=fields | {"consumed_quantity": "1"})
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(client.get(item_url, headers=auth).json()["on_hand_milli"], before - 1000)
            ticket = client.get(f"/repairs/{repair_id}").text
            self.assertEqual(ticket.count('obi-entry-body">Part used: Synthetic &lt;capacitor&gt; × 1 pcs'), 1)
            missing = client.get(f"/repairs/{repair_id}", params={"part_q": "No such part"}).text
            parts = missing.split('aria-labelledby="stock-usage"', 1)[1].split('</section>', 1)[0]
            self.assertIn("No catalog part matches.", parts)
            self.assertRegex(parts, r'id="manual-description"[^>]*value="No such part"')

    def test_json_web_and_real_http_cli_share_snapshots_retry_and_restart(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT 1,'viewer','viewer',password_hash FROM users WHERE id=1")

        def cli(*args, stdin=None):
            result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                    text=True, capture_output=True, timeout=10, env=python_env())
            self.assertTrue(token not in result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout) if "--json" in args else result.stdout

        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}

            def send(url, body, key=None):
                return client.post(url, headers=auth | {"Idempotency-Key": key or uuid4().hex}, json=body)

            repair = send(API, DEVICE).json()
            item = send(INVENTORY_API, ITEM | {"purchase_cost_cents": 9137, "selling_price_cents": 1999}).json()
            item_url = f"{INVENTORY_API}/{item['id']}"
            self.assertEqual(send(f"{item_url}/receive", {"revision": 1, "quantity_milli": 10000})
                             .status_code, 201)
            repair_id = repair["id"]
            url = f"{API}/{repair_id}/consume-part"
            web = f"/repairs/{repair_id}/consume-part"
            payload = dict(item_id=item["id"], repair_revision=1, item_revision=2,
                           consumed_quantity_milli=2000, billed_quantity_milli=1500)

            def lose_response(response):
                self.assertEqual(response.status_code, 201)
                raise httpx.ReadError("Synthetic lost response")

            client.event_hooks["response"].append(lose_response)
            try:
                with self.assertRaises(httpx.ReadError):
                    send(url, payload, "api-use")
            finally:
                client.event_hooks["response"].remove(lose_response)
            first = send(url, payload, "api-use")
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(send(url, payload, "api-use").json(), first.json())

            selection = client.get(web)
            self.assertEqual(selection.status_code, 200)
            ticket = client.get(f"/repairs/{repair_id}").text
            parts_used = ticket.split('aria-labelledby="stock-usage"', 1)[1].split('</section>', 1)[0]
            self.assertIn(f'href="{web}"', parts_used)
            form_url = f"{web}/{item['id']}"
            self.assertIn(form_url, selection.text)
            form = form_fields(client.get(form_url), form_url) | {
                "consumed_quantity": "2", "billed_quantity": "1.5", "unit_price": "19.99",
                "work_note": "Connector fitted and checked.",
            }
            self.assertEqual(client.post(form_url, data=form | {"csrf": "bad"}).status_code, 403)
            invalid = client.post(form_url, data=form | {"consumed_quantity": "2.1234"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('value="2.1234"', invalid.text)
            self.assertIn(form["work_note"], invalid.text)
            self.assertNotIn(form["work_note"], client.get(f"/repairs/{repair_id}").text)
            tables = (*TABLES, "work_entries")
            before = [rows(self, table) for table in tables]
            too_long = client.post(form_url, data=form | {"work_note": "a" * 5001})
            self.assertEqual(too_long.status_code, 422)
            self.assertIn('id="work_note-error"', too_long.text)
            self.assertEqual([rows(self, table) for table in tables], before)
            saved = client.post(form_url, data=form)
            self.assertEqual(saved.status_code, 303)
            self.assertNotIn("#", saved.headers["location"])
            self.assertEqual(client.post(form_url, data=form).status_code, 303)
            ticket = client.get(f"/repairs/{repair_id}").text
            self.assertEqual(sum(form["work_note"] in body for (body,) in self.snapshot(
                "SELECT body FROM work_entries WHERE repair_id=?", (repair_id,))), 1)
            self.assertIn("Part used: Synthetic &lt;capacitor&gt; × 2 pcs", ticket)
            changed_note = client.post(form_url, data=form | {"work_note": "Different note"})
            self.assertEqual(changed_note.status_code, 409)
            stale = client.post(form_url, data=form | {"key": "stale-web"})
            self.assertEqual(stale.status_code, 409)
            self.assertIn("Open latest version", stale.text)

            command = ("repairs", "consume-part", str(repair_id), str(item["id"]), "--repair-revision", "1",
                       "--item-revision", "4", "--consumed-quantity", "2", "--billed-quantity", "1.5",
                       "--unit-price", "19.99", "--key", "cli-use", "--json")
            third = cli(*command)
            self.assertEqual(cli(*command), third)
            # The same key from another principal is a new request against stale revisions.
            self.assertEqual(send(url, payload | {"item_revision": 4, "unit_price_cents": 1999},
                                  "cli-use").status_code, 409)
            self.assertEqual(send(f"{API}/{repair_id}/charges", {
                "kind": "labor", "description": "Bench work", "quantity_milli": 1000, "unit_price_cents": 1000,
            }).status_code, 201)
            self.assertEqual(client.put(f"{API}/{repair_id}/discount", headers=auth | {"Idempotency-Key": "discount"},
                                        json={"revision": 0, "kind": "percent", "value": 10}).status_code, 200)
            self.assertEqual(client.patch(item_url, headers=auth | {"Idempotency-Key": "catalog-edit"},
                                          json=ITEM | {"revision": 5, "description": "New name",
                                                       "selling_price_cents": 2500}).status_code, 200)
            self.assertEqual(send(url, payload, "api-use").json(), first.json())

        with workspace_client(self.workspace) as (client, _):
            charges_url = f"{API}/{repair_id}/charges"
            legacy = client.get(charges_url, headers=auth).json()
            self.assertNotIn("movement_id", legacy["items"][0])
            self.assertEqual(cli("repairs", "charges", str(repair_id), "--json"), legacy)
            summary = cli("repairs", "charges", str(repair_id), "--include-consumption", "--json")
            self.assertEqual(summary, client.get(charges_url, params={"include_consumption": "true"},
                                                 headers=auth).json())
            charge_url = f"{charges_url}/{first.json()['id']}"
            self.assertEqual(client.get(charge_url, headers=auth).json(), legacy["items"][0])
            self.assertEqual(client.get(charge_url, params={"include_consumption": "true"},
                                        headers=auth).json(), first.json())
            self.assertEqual((summary["parts_subtotal_cents"], summary["labor_subtotal_cents"],
                              summary["discount_cents"], summary["total_cents"]), (8997, 1000, 1000, 8997))
            for charge in summary["items"][:3]:
                self.assertEqual((charge["description"], charge["unit"], charge["unit_cost_cents"],
                                  charge["unit_price_cents"], charge["consumed_quantity_milli"],
                                  charge["quantity_milli"], charge["line_total_cents"]),
                                 (ITEM["description"], "pcs", 9137, 1999, 2000, 1500, 2999))
            self.assertEqual(cli("inventory", "show", str(item["id"]), "--json")["on_hand_milli"], 4000)
            self.assertEqual(cli("inventory", "reconcile", "--json")["mismatches"], [])
            self.assertEqual(self.snapshot(
                "SELECT actor_username, source, token_label FROM audit_events "
                "WHERE operation='inventory.consume' ORDER BY id"),
                [("owner", "api", "Workshop CLI"), ("owner", "web", None), ("cli", "cli", None)])
            self.sign_in(client, "viewer")
            viewer = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
            viewer_auth = {"Authorization": f"Bearer {viewer}", "Idempotency-Key": "view-use"}
            self.assertEqual(client.post(url, headers=viewer_auth, json=payload).status_code, 403)
            visible = client.get(charges_url, params={"include_consumption": "true"}, headers=viewer_auth).json()
            self.assertIsNone(visible["items"][0]["unit_cost_cents"])
            self.assertEqual(visible["items"][0]["consumed_quantity_milli"], 2000)
            self.assertEqual(visible["total_cents"], 8997)
            self.assertEqual(client.get(web).status_code, 403)
            self.assertEqual(client.get(form_url).status_code, 403)
            detail = client.get(f"/repairs/{repair_id}")
            self.assertNotIn("91.37", detail.text)
            self.assertNotIn("Use catalog part", detail.text)
