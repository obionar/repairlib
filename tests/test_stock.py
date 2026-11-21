"""S4 stock ledger: receipts, reasoned adjustments and compensating reversals."""

from contextlib import closing
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from repairlib.contracts import ItemInput
from repairlib.domain.customers import CustomerAccess
from repairlib.domain.tokens import api_token_hash
from repairlib.lifecycle.workspace import LOCK, database_path
from repairlib.storage import initialization

TABLES = ("inventory_items", "inventory_movements", "stock_requests", "audit_events")


class StockTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        self.workspace.mkdir()
        self.path = database_path(self.workspace)
        self.path.parent.mkdir()
        initialization.initialize_database(self.path, "Shop", "UTC", "owner", "unused")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Other shop')")
            db.executemany(
                "INSERT INTO users(id,shop_id,username,role,password_hash) VALUES (?,?,?,?,'unused')",
                [(2, 1, "tech", "technician"), (3, 1, "viewer", "viewer"), (4, 2, "other", "admin")],
            )
        self.owner, self.tech = self.token(1), self.token(2)
        self.viewer, self.other = self.token(3, "read"), self.token(4)
        from repairlib.domain import inventory
        self.item = inventory.create_item(self.workspace, self.owner,
                                          ItemInput(sku="FUSE", description="Fuse", unit="pcs"), key="item")

    def token(self, user_id, scope="write"):
        secret = "rl_" + secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key)
                   SELECT shop_id,id,revision,?,'Synthetic stock',?,? FROM users WHERE id=?""",
                (api_token_hash(secret), scope, scope, user_id),
            )
        return CustomerAccess("api", secret)

    def snapshot(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, args).fetchall()

    def state(self):
        return [self.snapshot(f"SELECT * FROM {table}") for table in TABLES]

    def refused(self, code, operation):
        from repairlib.domain import stock
        with self.assertRaises(stock.StockError) as captured:
            operation()
        self.assertEqual(captured.exception.code, code, captured.exception)

    def test_receive_adjust_reverse_refusals_replay_and_audit(self):
        from repairlib.domain import inventory, stock
        ws, item_id = self.workspace, self.item.id

        received = stock.receive_stock(ws, self.tech, item_id, 5000, revision=1, key="r1", reason="Delivery 7")
        self.assertEqual((received.item.on_hand_milli, received.item.revision), (5000, 2))
        self.assertEqual((received.movement.kind, received.movement.quantity_milli,
                          received.movement.on_hand_after_milli, received.movement.reason,
                          received.movement.actor_username), ("receive", 5000, 5000, "Delivery 7", "tech"))
        # Replay returns the stored result; a different body under the key conflicts.
        self.assertEqual(stock.receive_stock(ws, self.tech, item_id, 5000, revision=1, key="r1",
                                             reason="Delivery 7"), received)
        self.refused("conflict", lambda: stock.receive_stock(ws, self.tech, item_id, 6000, revision=1, key="r1"))

        before = self.state()
        self.refused("stale_revision", lambda: stock.receive_stock(ws, self.tech, item_id, 1, revision=1, key="s"))
        self.refused("insufficient_stock", lambda: stock.adjust_stock(
            ws, self.tech, item_id, -5001, "Count", revision=2, key="neg"))
        self.refused("validation", lambda: stock.adjust_stock(ws, self.tech, item_id, -1, " ", revision=2, key="v"))
        self.refused("validation", lambda: stock.adjust_stock(ws, self.tech, item_id, 0, "Zero", revision=2, key="v"))
        self.refused("validation", lambda: stock.receive_stock(ws, self.tech, item_id, -1, revision=2, key="v"))
        self.refused("validation", lambda: stock.receive_stock(ws, self.tech, item_id, 1.5, revision=2, key="v"))
        self.refused("forbidden", lambda: stock.receive_stock(ws, self.viewer, item_id, 1, revision=2, key="v"))
        self.refused("unauthenticated", lambda: stock.receive_stock(ws, self.other, item_id, 1, revision=2, key="v"))
        self.refused("unit_locked", lambda: inventory.edit_item(
            ws, self.owner, item_id, ItemInput(sku="FUSE", description="Fuse", unit="m"), revision=2, key="u"))
        with patch("repairlib.storage.repairs.audit", side_effect=sqlite3.IntegrityError("Synthetic failure")):
            with self.assertRaises(sqlite3.IntegrityError):
                stock.adjust_stock(ws, self.tech, item_id, -1000, "Damaged", revision=2, key="fail")
        self.assertEqual(self.state(), before)

        adjusted = stock.adjust_stock(ws, self.tech, item_id, -1500, "Damaged in drawer", revision=2, key="a1")
        self.assertEqual((adjusted.item.on_hand_milli, adjusted.movement.on_hand_after_milli,
                          adjusted.movement.kind), (3500, 3500, "adjust"))
        # The receipt cannot be reversed while consumed stock would go negative.
        self.refused("insufficient_stock", lambda: stock.reverse_movement(
            ws, self.tech, item_id, received.movement.id, "Wrong item", revision=3, key="rv0"))
        reversed_adjustment = stock.reverse_movement(ws, self.owner, item_id, adjusted.movement.id,
                                                     "Found in drawer", revision=3, key="rv1")
        self.assertEqual((reversed_adjustment.movement.kind, reversed_adjustment.movement.quantity_milli,
                          reversed_adjustment.movement.reverses_id, reversed_adjustment.item.on_hand_milli),
                         ("reversal", 1500, adjusted.movement.id, 5000))
        self.refused("already_reversed", lambda: stock.reverse_movement(
            ws, self.owner, item_id, adjusted.movement.id, "Again", revision=4, key="rv2"))
        self.refused("not_reversible", lambda: stock.reverse_movement(
            ws, self.owner, item_id, reversed_adjustment.movement.id, "Undo undo", revision=4, key="rv3"))

        # Other-shop items and movements are invisible to this shop.
        with closing(sqlite3.connect(self.path)) as db, db:
            outside = db.execute("INSERT INTO inventory_items(shop_id,sku,description,unit) "
                                 "VALUES (2,'FUSE','Outside','pcs')").lastrowid
        self.refused("not_found", lambda: stock.receive_stock(ws, self.owner, outside, 1, revision=1, key="o"))
        self.refused("not_found", lambda: stock.list_movements(ws, self.owner, outside))
        self.refused("not_found", lambda: stock.reverse_movement(
            ws, self.owner, outside, received.movement.id, "Cross", revision=1, key="o2"))

        page = stock.list_movements(ws, self.viewer, item_id, limit=2)
        self.assertEqual([m.kind for m in page.movements], ["reversal", "adjust"])
        self.assertEqual(page.movements[1].reversed_by_id, reversed_adjustment.movement.id)
        self.assertEqual([m.kind for m in stock.list_movements(ws, self.viewer, item_id,
                                                               before=page.next_before).movements], ["receive"])
        self.assertEqual(stock.reconcile_stock(ws, self.viewer).mismatches, [])
        self.assertEqual(self.snapshot(
            """SELECT actor_user_id, token_label, operation, json_extract(changes, '$.quantity_milli'),
                      json_extract(changes, '$.on_hand_milli.after')
               FROM audit_events WHERE operation LIKE 'inventory.%' AND operation NOT IN
               ('inventory.create') ORDER BY id"""),
            [(2, "Synthetic stock", "inventory.receive", 5000, 5000),
             (2, "Synthetic stock", "inventory.adjust", -1500, 3500),
             (1, "Synthetic stock", "inventory.reverse", 1500, 5000)])
        archived = inventory.archive_item(ws, self.owner, item_id, revision=4, key="arch")
        self.refused("archived", lambda: stock.receive_stock(ws, self.owner, item_id, 1,
                                                             revision=archived.revision, key="late"))

    def test_concurrent_connections_never_lose_or_oversell_stock(self):
        from repairlib.domain import inventory, stock
        ws, item_id = self.workspace, self.item.id
        stock.receive_stock(ws, self.owner, item_id, 5000, revision=1, key="seed")
        workers = 6
        barrier = threading.Barrier(workers)
        outcomes = []

        def oversell(number):
            barrier.wait()
            try:
                stock.adjust_stock(ws, self.tech, item_id, -4000, "Count", revision=2, key=f"o{number}")
                outcomes.append("ok")
            except stock.StockError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=oversell, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes), ["ok"] + ["stale_revision"] * (workers - 1))
        self.assertEqual(inventory.read_item(ws, self.owner, item_id).on_hand_milli, 1000)

        barrier = threading.Barrier(workers)

        def receive(number):
            barrier.wait()
            for _ in range(200):
                revision = inventory.read_item(ws, self.owner, item_id).revision
                try:
                    stock.receive_stock(ws, self.tech, item_id, 250, revision=revision, key=f"c{number}")
                    return
                except stock.StockError as error:
                    if error.code != "stale_revision":
                        raise

        threads = [threading.Thread(target=receive, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        item = inventory.read_item(ws, self.owner, item_id)
        self.assertEqual((item.on_hand_milli, item.revision), (1000 + workers * 250, 3 + workers))
        self.assertEqual(self.snapshot("SELECT COUNT(*), SUM(quantity_milli) FROM inventory_movements"),
                         [(2 + workers, item.on_hand_milli)])
        self.assertEqual(stock.reconcile_stock(ws, self.owner).mismatches, [])

    def test_restart_readback_reconciliation_and_doctor_mismatch(self):
        from repairlib.doctor import diagnose_workspace
        from repairlib.domain import inventory, stock
        ws, item_id = self.workspace, self.item.id
        stock.receive_stock(ws, self.owner, item_id, 2500, revision=1, key="r")
        stock.adjust_stock(ws, self.owner, item_id, 500, "Recount", revision=2, key="a")
        (ws / LOCK).touch(0o600)  # setup normally creates the maintenance lock file
        report = diagnose_workspace(ws)
        self.assertEqual((report.stock_mismatches, report.exit_status), (0, 0))
        self.assertIn("Stock ledger mismatches: 0", report.lines())
        # A fresh connection (restart) reads the same committed ledger and cache.
        self.assertEqual(inventory.read_item(ws, self.owner, item_id).on_hand_milli, 3000)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE inventory_items SET on_hand_milli = 999 WHERE id = ?", (item_id,))
        result = stock.reconcile_stock(ws, self.owner)
        self.assertEqual([(m.item_id, m.cached_milli, m.ledger_milli) for m in result.mismatches],
                         [(item_id, 999, 3000)])
        self.assertEqual(result.items_checked, 1)
        report = diagnose_workspace(ws)
        self.assertEqual((report.stock_mismatches, report.exit_status), (1, 1))
        self.assertIn(f"Stock ledger mismatch item IDs: {item_id}", report.lines())

    def test_schema_17_items_upgrade_with_empty_ledger(self):
        from repairlib.domain import inventory, stock
        root = self.workspace.parent / "prior"
        root.mkdir()
        path = database_path(root)
        path.parent.mkdir()
        initialization.initialize_database(path, "Prior", "UTC", "owner", "unused", target=17)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO inventory_items(shop_id,sku,description,unit) VALUES (1,'OLD','Old','pcs')")
        initialization.upgrade_database(path)
        self.assertEqual(initialization.schema_details(path), ("initialized", initialization.SCHEMA_VERSION))
        self.workspace, self.path = root, path
        access = self.token(1)
        (item,) = inventory.list_items(root, access).items
        self.assertEqual((item.sku, item.on_hand_milli, item.revision), ("OLD", 0, 1))
        self.assertEqual(stock.receive_stock(root, access, item.id, 1, revision=1, key="k").item.on_hand_milli, 1)
        with closing(sqlite3.connect(path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE inventory_items SET unit = 'm' WHERE id = ?", (item.id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE inventory_items SET on_hand_milli = -1 WHERE id = ?", (item.id,))


if __name__ == "__main__":
    unittest.main()
