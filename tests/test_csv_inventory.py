"""S5 catalog and opening-stock CSV gates through the admin API and HTTP CLI."""

from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import subprocess
import unittest

from repairlib.storage import initialization, stock_reconciliation
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_tokens import ACCOUNT, TokenFixture, issued_secret

CATALOG = "/api/v1/csv/catalog"
OPENING = "/api/v1/csv/opening-stock"
CATALOG_HEADER = (b"sku,description,category,manufacturer,part_number,package,specifications,bin,unit,"
                  b"low_stock_milli,purchase_cost_cents,selling_price_cents,opening_quantity_milli\r\n")
OPENING_HEADER = b"sku,quantity_milli,reason\r\n"
NEW_ITEMS = CATALOG_HEADER + (
    b'CAP-10,"Capacitor, 10 uF",Passive,Acme,C10,0805,"16 V\nX7R",A1,pcs,2000,15,40,2500\r\n'
    b"FUSE-2,Fuse 2 A,,,,,,B2,pcs,,,120,\r\n"
)
OPENING_ROWS = OPENING_HEADER + b"EX-1,1500,Counted shelf\r\n"


class InventoryCsvTests(TokenFixture, unittest.TestCase):
    def authorization(self, api, scope="write"):
        if not api.cookies:
            self.sign_in(api)
        response = api.post(ACCOUNT, data=self.issue_form(api, scope=scope, name=f"CSV {scope}"))
        return {"Authorization": f"Bearer {issued_secret(response)}"}

    def preview(self, api, auth, path, body):
        response = api.post(path + "/preview", content=body, headers={**auth, "Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, api, auth, path, review, body, key):
        return api.post(path + "/apply", content=body,
                        params={field: review[field] for field in ("preview_id", "sha256")},
                        headers={**auth, "Content-Type": "text/csv", "Idempotency-Key": key})

    def execute(self, *statements):
        with closing(sqlite3.connect(self.db)) as db, db:
            for statement in statements:
                db.execute(statement)

    def seed_existing(self):
        self.execute("INSERT INTO inventory_items(id, shop_id, sku, description, unit) "
                     "VALUES (10, 1, 'EX-1', 'Existing part', 'pcs')")

    def test_cli_catalog_and_opening_stock_rollback_ledger_and_replay_after_restart(self):
        catalog_path = self.workspace.parent / "catalog.csv"
        opening_path = self.workspace.parent / "opening.csv"
        self.seed_existing()

        def cli(kind, *args, status=0):
            result = subprocess.run([*COMMAND, "csv", kind, *args, "--workspace", str(self.workspace)],
                                    env=python_env(), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, status, result.stderr)
            return result.stdout

        def options(kind, path, review, key):
            return (kind, "apply", str(path), "--preview-id", review["preview_id"],
                    "--sha256", review["sha256"], "--key", key, "--json")

        with workspace_client(self.workspace) as (api, _):
            auth = self.authorization(api)
            self.assertEqual(cli("catalog", "template"), CATALOG_HEADER.decode().replace("\r\n", "\n"))
            self.assertEqual(cli("opening-stock", "template"), OPENING_HEADER.decode().replace("\r\n", "\n"))
            catalog_path.write_bytes(NEW_ITEMS)
            opening_path.write_bytes(OPENING_ROWS)
            catalog = json.loads(cli("catalog", "preview", str(catalog_path), "--json"))
            self.assertEqual(catalog["errors"], [])
            self.assertEqual(catalog["sha256"], sha256(NEW_ITEMS).hexdigest())
            self.assertEqual([(a["row"], a["action"], a["fields"]["sku"]) for a in catalog["actions"]],
                             [(2, "create", "CAP-10"), (4, "create", "FUSE-2")])
            opening = json.loads(cli("opening-stock", "preview", str(opening_path), "--json"))
            self.assertEqual(opening["errors"], [])
            self.assertEqual(opening["actions"], [{"row": 2, "action": "receive", "fields": {
                "sku": "EX-1", "item_id": "10", "item_revision": "1",
                "quantity_milli": "1500", "reason": "Counted shelf"}}])

            # A failed ledger or audit write rolls back items, movements, receipt and history.
            self.execute("""CREATE TRIGGER reject_movement BEFORE INSERT ON inventory_movements
                            BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
            cli(*options("catalog", catalog_path, catalog, "catalog-batch"), status=3)
            self.execute("DROP TRIGGER reject_movement",
                         """CREATE TRIGGER reject_audit BEFORE INSERT ON audit_events
                            WHEN NEW.operation = 'inventory.opening_stock'
                            BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
            cli(*options("opening-stock", opening_path, opening, "opening-batch"), status=3)
            self.assertEqual(self.snapshot("SELECT id, on_hand_milli, revision FROM inventory_items"),
                             [(10, 0, 1)])
            for table in ("inventory_movements", "csv_requests", "audit_events WHERE entity_type='inventory_item'"):
                self.assertEqual(self.snapshot(f"SELECT COUNT(*) FROM {table}"), [(0,)], table)
            self.execute("DROP TRIGGER reject_audit")

            created = json.loads(cli(*options("catalog", catalog_path, catalog, "catalog-batch")))
            stocked = json.loads(cli(*options("opening-stock", opening_path, opening, "opening-batch")))
            self.assertEqual(stocked["rows"], [{"row": 2, "id": 10, "revision": 2}])
            capacitor, fuse = created["rows"]
            self.assertEqual((capacitor["row"], capacitor["revision"]), (2, 2))
            self.assertEqual((fuse["row"], fuse["revision"]), (4, 1))

        with workspace_client(self.workspace) as (api, _):
            self.assertEqual(json.loads(cli(*options("catalog", catalog_path, catalog, "catalog-batch"))), created)
            self.assertEqual(json.loads(cli(*options("opening-stock", opening_path, opening, "opening-batch"))),
                             stocked)
            changed = OPENING_ROWS.replace(b"1500", b"1600")
            self.assertEqual(self.apply(api, auth, OPENING, opening, changed, "opening-batch").status_code, 409)
            item = api.get(f"/api/v1/inventory/{capacitor['id']}", headers=auth).json()
            self.assertEqual((item["description"], item["specifications"], item["on_hand_milli"],
                              item["low_stock_milli"], item["purchase_cost_cents"], item["selling_price_cents"]),
                             ("Capacitor, 10 uF", "16 V\nX7R", 2500, 2000, 15, 40))
            fuse_item = api.get(f"/api/v1/inventory/{fuse['id']}", headers=auth).json()
            self.assertEqual((fuse_item["on_hand_milli"], fuse_item["low_stock_milli"],
                              fuse_item["purchase_cost_cents"]), (0, 0, None))
            moves = api.get("/api/v1/inventory/10/movements", headers=auth).json()["movements"]
            self.assertEqual([(m["kind"], m["quantity_milli"], m["on_hand_after_milli"], m["reason"],
                               m["actor_username"]) for m in moves],
                             [("receive", 1500, 1500, "Counted shelf", "cli")])
            reconciliation = api.get("/api/v1/inventory/reconciliation", headers=auth).json()
            self.assertEqual((reconciliation["items_checked"], reconciliation["mismatches"],
                              reconciliation["issues"]), (3, [], []))
        self.assertEqual(self.snapshot(
            "SELECT item_id, kind, quantity_milli, reason FROM inventory_movements ORDER BY id"),
            [(capacitor["id"], "receive", 2500, "Opening stock"), (10, "receive", 1500, "Counted shelf")])
        self.assertEqual(self.snapshot(
            "SELECT actor_username, source, token_label, operation, entity_id FROM audit_events "
            "WHERE entity_type='inventory_item' ORDER BY id"), [
                ("cli", "cli", None, "inventory.create", capacitor["id"]),
                ("cli", "cli", None, "inventory.opening_stock", capacitor["id"]),
                ("cli", "cli", None, "inventory.create", fuse["id"]),
                ("cli", "cli", None, "inventory.opening_stock", 10)])
        changes = json.loads(self.snapshot("SELECT changes FROM audit_events WHERE entity_id=10")[0][0])
        self.assertEqual((changes["quantity_milli"], changes["on_hand_milli"], changes["kind"]),
                         (1500, {"before": 0, "after": 1500}, "receive"))

    def test_sku_quantity_refusals_stale_revisions_and_key_conflict(self):
        self.seed_existing()
        self.execute(
            "INSERT INTO shops(id, name) VALUES (2, 'Outside')",
            "INSERT INTO inventory_items(id, shop_id, sku, description, unit) VALUES (20, 2, 'OUT-1', 'Other', 'pcs')",
            "INSERT INTO inventory_items(id, shop_id, sku, description, unit, archived_at) "
            "VALUES (11, 1, 'OLD-1', 'Archived', 'pcs', '2026-01-01')",
            "INSERT INTO inventory_items(id, shop_id, sku, description, unit) VALUES (12, 1, 'MOVED', 'Moved', 'pcs')",
        )
        with self.client() as api:
            auth = self.authorization(api)
            read = self.authorization(api, "read")
            self.assertEqual(api.get(OPENING + "/template", headers=read).content, OPENING_HEADER)
            self.assertEqual(api.post(CATALOG + "/preview", headers=read, content=NEW_ITEMS).status_code, 403)
            moved = api.post("/api/v1/inventory/12/receive", json={"quantity_milli": 1000, "revision": 1},
                             headers={**auth, "Idempotency-Key": "moved"})
            self.assertEqual(moved.status_code, 201, moved.text)

            def row(sku, quantity="", unit="pcs", price="1"):
                return f"{sku},Part,,,,,,,{unit},,,{price},{quantity}\r\n".encode()

            catalog = self.preview(api, auth, CATALOG, CATALOG_HEADER + b"".join([
                row("DUP"), row("DUP"), row("EX-1"), row("OLD-1"), row("NEG", "-5"), row("DEC", "1.5"),
                row("ZERO", "0"), row("NOUNIT", unit=""), row("NOPRICE", price=""), row("OUT-1", "1000"),
            ]))
            self.assertIsNone(catalog["preview_id"])
            self.assertEqual(sorted({(e["row"], e["field"]) for e in catalog["errors"]}), [
                (2, "sku"), (3, "sku"), (4, "sku"), (5, "sku"), (6, "opening_quantity_milli"),
                (7, "opening_quantity_milli"), (8, "opening_quantity_milli"), (9, "unit"),
                (10, "selling_price_cents")])
            self.assertEqual([a["fields"]["sku"] for a in catalog["actions"]], ["OUT-1"])

            opening = self.preview(api, auth, OPENING, OPENING_HEADER + (
                b"NOPE,1,\r\nOUT-1,1,\r\nOLD-1,1,\r\nMOVED,1,\r\nEX-1,-1,\r\nEX-1,1,\r\n"
                b"DUPE,0,\r\nZERO-X,,\r\n"
            ))
            self.assertIsNone(opening["preview_id"])
            self.assertEqual(sorted({(e["row"], e["field"]) for e in opening["errors"]}), [
                (2, "sku"), (3, "sku"), (4, "sku"), (5, "sku"), (6, "quantity_milli"), (6, "sku"),
                (7, "sku"), (8, "quantity_milli"), (8, "sku"), (9, "quantity_milli"), (9, "sku")])
            for body in (OPENING_HEADER + b"EX-1,1,x\r\nEX-1\r\n", OPENING_HEADER + b"EX-1,1," + b"r" * 201):
                self.assertIsNone(self.preview(api, auth, OPENING, body)["preview_id"])

            # A reviewed item revision change after preview refuses the whole batch.
            review = self.preview(api, auth, OPENING, OPENING_ROWS)
            self.execute("UPDATE inventory_items SET description='Edited', revision=revision+1 WHERE id=10")
            stale = self.apply(api, auth, OPENING, review, OPENING_ROWS, "stale")
            self.assertEqual(stale.json()["error"]["code"], "stale_revision")
            review = self.preview(api, auth, OPENING, OPENING_ROWS)
            self.assertEqual(review["actions"][0]["fields"]["item_revision"], "2")
            self.execute("INSERT INTO inventory_movements(shop_id, item_id, kind, quantity_milli, "
                         "on_hand_after_milli, reason, actor_user_id) VALUES (1, 10, 'receive', 5, 5, '', 1)",
                         "UPDATE inventory_items SET on_hand_milli=5, revision=revision+1 WHERE id=10")
            moved_after = self.apply(api, auth, OPENING, review, OPENING_ROWS, "moved-after")
            self.assertEqual(moved_after.status_code, 409)
            self.assertIn("row.2.sku", moved_after.json()["error"]["fields"])

            # A SKU created after preview is rechecked; nothing from the batch is kept.
            batch = CATALOG_HEADER + row("NEW-1", "1000") + row("NEW-2")
            review = self.preview(api, auth, CATALOG, batch)
            self.execute("INSERT INTO inventory_items(shop_id, sku, description, unit) VALUES (1, 'NEW-2', 'Raced', 'pcs')")
            raced = self.apply(api, auth, CATALOG, review, batch, "raced")
            self.assertEqual(raced.status_code, 409)
            self.assertIn("row.3.sku", raced.json()["error"]["fields"])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_items WHERE sku='NEW-1'"), [(0,)])

            good = CATALOG_HEADER + row("NEW-3", "1000")
            review = self.preview(api, auth, CATALOG, good)
            self.assertEqual(self.apply(api, auth, CATALOG, review, good, "catalog-key").status_code, 201)
            other = CATALOG_HEADER + row("NEW-4")
            review = self.preview(api, auth, CATALOG, other)
            conflict = self.apply(api, auth, CATALOG, review, other, "catalog-key")
            self.assertEqual(conflict.json()["error"]["code"], "conflict")
        self.assertEqual(self.snapshot("SELECT item_id, quantity_milli FROM inventory_movements ORDER BY id"),
                         [(12, 1000), (10, 5), (self.snapshot("SELECT id FROM inventory_items WHERE sku='NEW-3'")[0][0],
                                                 1000)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_items WHERE sku IN ('NEW-1','NEW-4')"), [(0,)])
        self.assertEqual(self.snapshot("SELECT shop_id, on_hand_milli FROM inventory_items WHERE sku='OUT-1'"),
                         [(2, 0)])

    def test_schema_22_inventory_and_ledger_survive_upgrade(self):
        prior = self.workspace.parent / "prior.sqlite3"
        initialization.initialize_database(prior, "Prior shop", "UTC", "owner", "unused", target=22)
        with closing(sqlite3.connect(prior)) as db, db:
            db.execute("INSERT INTO inventory_items(id, shop_id, sku, description, unit, on_hand_milli, revision) "
                       "VALUES (1, 1, 'PRIOR', 'Prior part', 'pcs', 750, 2)")
            db.execute("INSERT INTO inventory_movements(shop_id, item_id, kind, quantity_milli, "
                       "on_hand_after_milli, reason, actor_user_id) VALUES (1, 1, 'receive', 750, 750, '', 1)")
        initialization.upgrade_database(prior)
        with closing(sqlite3.connect(prior)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], initialization.SCHEMA_VERSION)
            self.assertEqual([tuple(r) for r in db.execute(
                "SELECT sku, on_hand_milli, revision FROM inventory_items")], [("PRIOR", 750, 2)])
            result = stock_reconciliation.reconcile(db, None, initialization.SCHEMA_VERSION)
            self.assertEqual((result.items_checked, result.mismatches, result.issues), (1, [], []))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
