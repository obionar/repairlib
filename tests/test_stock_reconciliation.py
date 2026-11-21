"""S4 completion gates for the shared read-only inventory report and recovery."""

from contextlib import closing
from dataclasses import asdict
import json
import sqlite3
import subprocess
import unittest

from repairlib.contracts import ItemInput, PartConsumption
from repairlib.domain import attachments, charges, inventory, stock
from repairlib.lifecycle.workspace import LOCK, database_path
from repairlib.storage.initialization import SCHEMA_VERSION
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
import test_repair_returns as fixture


RECONCILIATION = "/api/v1/inventory/reconciliation"


class StockReconciliationTests(unittest.TestCase):
    token = fixture.ReturnTests.token
    snapshot = fixture.ReturnTests.snapshot
    use = fixture.ReturnTests.use
    physical = fixture.ReturnTests.physical
    combined = fixture.ReturnTests.combined
    visibility = fixture.ReturnTests.visibility
    correction = fixture.ReturnTests.correction

    def setUp(self):
        fixture.ReturnTests.setUp(self)
        (self.workspace / LOCK).touch(0o600)
        for directory in (self.workspace, self.path.parent):
            directory.chmod(0o700)
        self.path.chmod(0o600)

    def command(self, *args, code=0, stdin=None):
        result = subprocess.run([*COMMAND, *args], text=True, input=stdin, capture_output=True,
                                timeout=20, env=python_env())
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return result.stdout

    def doctor(self, code=0, workspace=None):
        return self.command("doctor", "--workspace", str(workspace or self.workspace), code=code)

    def adapters(self, code):
        with workspace_client(self.workspace) as (client, _):
            self.assertEqual(client.get(RECONCILIATION).status_code, 401)
            self.assertEqual(client.get(RECONCILIATION, headers={
                "Authorization": f"Bearer {self.other.credential}"}).status_code, 401)
            response = client.get(RECONCILIATION, headers={
                "Authorization": f"Bearer {self.viewer.credential}"})
            self.assertEqual(response.status_code, 200)
            args = ("inventory", "reconcile", "--workspace", str(self.workspace))
            self.assertEqual(json.loads(self.command(*args, "--json", code=code)), response.json())
            human = self.command(*args, code=code)
            for issue in response.json()["issues"]:
                self.assertIn(issue["code"], human)
        return response.json()

    def test_healthy_and_damaged_read_only_adapters(self):
        self.correction()
        self.visibility(2, True)
        self.physical()
        self.visibility(3, False)
        self.physical("second-return", item_revision=4, quantity_milli=500)
        inventory.edit_item(self.workspace, self.owner, self.item.id,
                            ItemInput(sku="FUSE", description="New catalog description", purchase_cost_cents=300),
                            revision=5, key="catalog-edit")
        before = self.path.read_bytes()
        self.assertIn("Stock ledger issues: 0", self.doctor())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.adapters(0), {"items_checked": 1, "mismatches": [], "issues": []})

        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO inventory_items(shop_id, sku, description, unit, on_hand_milli) "
                       "VALUES (2, 'PRIVATE', 'Second shop cache', 'pcs', 7)")
            db.execute("DROP TRIGGER inventory_movements_append_only_update")
            db.execute("UPDATE inventory_movements SET on_hand_after_milli = 4999 WHERE id = 1")
            db.execute("UPDATE inventory_items SET on_hand_milli = 7500 WHERE id = ?", (self.item.id,))
            db.execute("UPDATE repair_charges SET description = 'Broken snapshot' WHERE id = ?", (self.charge.id,))
            db.execute(
                """INSERT INTO inventory_movements
                    (shop_id, item_id, kind, quantity_milli, on_hand_after_milli, reason, reverses_id, actor_user_id)
                VALUES (1, ?, 'return', 5000, 7500, 'Impossible return', ?, 2)""",
                (self.item.id, self.charge.movement_id),
            )
        before = self.path.read_bytes()
        doctor = self.doctor(code=1)
        self.assertIn("Stock ledger mismatches: 1", doctor)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.adapters(1)
        self.assertEqual((result["items_checked"], result["mismatches"]), (1, []))
        codes = {issue["code"] for issue in result["issues"]}
        self.assertEqual(codes, {"movement_balance", "consumption_snapshot", "return_snapshot", "over_return"})
        for code in codes:
            self.assertIn(code, doctor)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_duplicate_wrong_kind_and_cross_item_links(self):
        physical = self.physical()
        second = self.use(self.tech, self.second, "other-use", item_revision=4,
                          consumed_quantity_milli=1000)
        other_item = inventory.create_item(self.workspace, self.owner,
                                           ItemInput(sku="OTHER", description="Other"), key="other-item")
        with closing(sqlite3.connect(self.path)) as db, db:
            # Simulate malformed imported rows beyond the normal uniqueness/FK guards.
            db.execute("ALTER TABLE repair_part_consumptions RENAME TO original_links")
            db.execute("CREATE TABLE repair_part_consumptions AS SELECT * FROM original_links")
            db.execute("DROP TABLE original_links")
            db.execute("DELETE FROM repair_part_consumptions WHERE charge_id = ?", (second.id,))
            db.execute("INSERT INTO repair_part_consumptions VALUES (1, ?, ?)",
                       (self.charge.id, self.charge.movement_id))
            db.execute("INSERT INTO repair_part_consumptions VALUES (1, 999, 1)")
            db.execute("INSERT INTO repair_part_consumptions VALUES (1, ?, 999)", (second.id,))
            db.execute("DROP TRIGGER inventory_movements_append_only_update")
            db.execute("UPDATE inventory_movements SET item_id = ? WHERE id = ?",
                       (other_item.id, physical.stock.movement.id))
        result = stock.reconcile_stock(self.workspace, self.viewer)
        codes = {(issue.code, issue.movement_id, issue.charge_id) for issue in result.issues}
        self.assertIn(("consumption_link_count", self.charge.movement_id, None), codes)
        self.assertIn(("duplicate_charge_link", self.charge.movement_id, self.charge.id), codes)
        self.assertIn(("consumption_link_count", second.movement_id, None), codes)
        self.assertIn(("invalid_consumption_link", 1, 999), codes)
        self.assertIn(("invalid_consumption_link", 999, second.id), codes)
        self.assertIn(("invalid_return_link", physical.stock.movement.id, None), codes)

    def test_populated_backup_verify_restore_and_replay(self):
        item_id = self.item.id
        repair_id = self.first.id
        access = self.tech
        billed = self.correction()
        self.visibility(2, True)
        partial = self.physical()
        visible = self.visibility(3, False)
        adjusted = stock.adjust_stock(self.workspace, access, item_id, 250, "Recount", revision=4, key="adjust")
        second = self.use(access, self.second, "second-use", item_revision=5, consumed_quantity_milli=1000)
        mistake = self.combined(repair=self.second, charge_id=second.id, item_revision=6)
        payload = b"Synthetic repair attachment\n"

        def upload(workspace):
            staged = attachments.prepare_upload(workspace, access, repair_id, "note.txt", None)
            attachments.write_chunk(staged, payload)
            return attachments.commit_upload(workspace, access, repair_id, staged,
                                             filename="note.txt", entry_id=None, key="attachment")

        file = upload(self.workspace)
        ledger = stock.list_movements(self.workspace, access, item_id)
        item = inventory.read_item(self.workspace, access, item_id)
        summaries = [charges.charge_summary(self.workspace, access, repair.id)[0]
                     for repair in (self.first, self.second)]
        self.assertEqual([summary.total_cents for summary in summaries], [0, 0])
        self.assertEqual(item.on_hand_milli, 2250)
        self.assertEqual([move.kind for move in ledger.movements],
                         ["return", "consume", "adjust", "return", "consume", "receive"])
        self.assertEqual((billed.quantity_milli, partial.remaining_quantity_milli,
                          visible.deleted, mistake.charge.deleted), (500, 3000, False, True))

        archive = self.workspace.parent / f"schema-{SCHEMA_VERSION}.zip"
        restored = self.workspace.parent / f"restored schema-{SCHEMA_VERSION}"
        before = self.path.read_bytes()
        for args in (
            ("backup", "create", "--workspace", str(self.workspace), "--output", str(archive)),
            ("backup", "verify", str(archive)),
            ("backup", "restore", str(archive), "--workspace", str(restored)),
        ):
            self.assertIn(f"schema {SCHEMA_VERSION}", self.command(*args))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("Stock ledger issues: 0", self.doctor(workspace=restored))
        self.assertIn("schema is current", self.command("migrate", "--workspace", str(restored)))

        auth = {"Authorization": f"Bearer {access.credential}"}
        part = PartConsumption(item_id, self.first.revision, 2, 4000, 1500, 1999)
        base = f"/api/v1/repairs/{repair_id}"
        stock_base = f"/api/v1/inventory/{item_id}"
        restored_db = database_path(restored)
        with closing(sqlite3.connect(restored_db)) as db:
            before_replay = list(db.iterdump())
        with workspace_client(restored) as (client, _):
            self.assertEqual(client.get(RECONCILIATION, headers=auth).json(),
                             {"items_checked": 1, "mismatches": [], "issues": []})
            self.assertEqual(client.get(stock_base, headers=auth).json(), asdict(item))
            self.assertEqual(client.get(stock_base + "/movements", headers=auth).json(), asdict(ledger))
            for repair, summary in zip((self.first, self.second), summaries, strict=True):
                response = client.get(f"/api/v1/repairs/{repair.id}/charges", headers=auth,
                                      params={"include_consumption": "true"})
                self.assertEqual(response.json(), asdict(summary))
            self.assertEqual(client.get(base + f"/attachments/{file.id}/content", headers=auth).content, payload)
            response = client.post(base + "/consume-part", headers=auth | {"Idempotency-Key": "use"},
                                   json=asdict(part))
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json(), asdict(self.charge))
        source = self.workspace
        self.workspace = restored
        try:
            self.assertEqual(self.correction(), billed)
            self.assertEqual(self.physical(), partial)
            self.assertEqual(self.visibility(3, False), visible)
            self.assertEqual(self.combined(repair=self.second, charge_id=second.id, item_revision=6), mistake)
        finally:
            self.workspace = source
        self.assertEqual(stock.adjust_stock(restored, access, item_id, 250, "Recount",
                                           revision=4, key="adjust"), adjusted)
        received = stock.receive_stock(restored, self.owner, item_id, 5000, revision=1, key="receipt")
        self.assertEqual(received.movement, ledger.movements[-1])
        self.assertEqual(upload(restored), file)
        with closing(sqlite3.connect(restored_db)) as db:
            self.assertTrue(list(db.iterdump()) == before_replay, "Replays changed restored database rows")
