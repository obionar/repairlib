"""The inventory catalog contract through shared operations and explicit migration."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from repairlib.contracts import CustomerInput
from repairlib.domain.customers import CustomerAccess, create_customer, read_customer
from repairlib.domain.tokens import api_token_hash
from repairlib.lifecycle.workspace import database_path
from repairlib.storage import initialization

# Receipt tables gain a migration-managed age column; the gate compares business columns.
REQUESTS = ("SELECT shop_id, actor_id, operation, request_key, fingerprint, item_id, result "
            "FROM inventory_requests ORDER BY operation")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.path = database_path(self.workspace)
        self.path.parent.mkdir()

    def token(self, user_id, scope="write"):
        secret = "rl_" + secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key)
                   SELECT shop_id,id,revision,?,'Synthetic catalog',?,? FROM users WHERE id=?""",
                (api_token_hash(secret), scope, scope, user_id),
            )
        return CustomerAccess("api", secret)

    def snapshot(self, sql):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql).fetchall()

    def test_catalog_matrix(self):
        from repairlib.domain import inventory

        initialization.initialize_database(self.path, "Shop", "UTC", "owner", "unused")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Other shop')")
            db.executemany(
                "INSERT INTO users(id,shop_id,username,role,password_hash) VALUES (?,?,?,?,'unused')",
                [(2, 1, "tech", "technician"), (3, 1, "viewer", "viewer"), (4, 2, "other", "admin")],
            )
        owner, tech, viewer = self.token(1), self.token(2), self.token(3, "read")
        read_only, other = self.token(1, "read"), self.token(4)
        details = inventory.ItemInput(
            sku=" RC-1 ", description="Capacitor", category="Components",
            manufacturer="Synthetic", part_number="C10", package="0603",
            specifications="10 µF\r\n16 V", bin="A1", unit="pcs",
            low_stock_milli=1250, purchase_cost_cents=17, selling_price_cents=42,
        )

        def create(value=details, *, key="create", access=owner):
            return inventory.create_item(self.workspace, access, value, key=key)

        def edit(revision, *, item_id=None, access=tech, key="edit", value=None):
            return inventory.edit_item(self.workspace, access, first.id if item_id is None else item_id, value or updated_details,
                                       revision=revision, key=key)

        def archive(revision, *, item_id=None, access=tech, key="archive"):
            return inventory.archive_item(self.workspace, access, first.id if item_id is None else item_id,
                                          revision=revision, key=key)

        def read(item_id, access=owner):
            return inventory.read_item(self.workspace, access, item_id)

        def listing(access=owner, **kwargs):
            return inventory.list_items(self.workspace, access, **kwargs)

        def refused(code, operation):
            with self.assertRaises(inventory.InventoryError) as captured:
                operation()
            self.assertEqual(captured.exception.code, code)

        first = create()
        self.assertEqual((first.sku, first.revision, first.specifications), ("RC-1", 1, "10 µF\n16 V"))
        self.assertTrue(first.active)
        self.assertEqual(first.created_at, first.updated_at)
        self.assertEqual(create(replace(details, sku="RC-1")), first)

        # Authenticate as either local shop in turn, with an independently issued token.
        def local_shop(shop_id):
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE local_workspace SET shop_id = ?", (shop_id,))

        local_shop(2)
        second = create(access=other)
        self.assertEqual([item.id for item in listing(other).items], [second.id])
        refused("not_found", lambda: read(first.id, other))
        local_shop(1)
        self.assertEqual(listing().items, [first])
        self.assertEqual(read(first.id, viewer), replace(first, purchase_cost_cents=None))
        self.assertEqual(listing(viewer).items, [replace(first, purchase_cost_cents=None)])
        updated_details = replace(details, selling_price_cents=51, bin="B2")
        before = [self.snapshot(f"SELECT * FROM {table}")
                  for table in ("inventory_items", "inventory_requests", "audit_events")]
        refused("unauthenticated", lambda: create(access=other))
        refused("forbidden", lambda: edit(1, access=viewer))
        refused("forbidden", lambda: archive(1, access=viewer))
        refused("not_found", lambda: read(second.id))
        refused("not_found", lambda: edit(1, item_id=second.id, access=owner))
        refused("not_found", lambda: archive(1, item_id=second.id, access=owner))
        refused("forbidden", lambda: create(access=read_only, key="create"))
        refused("duplicate_sku", lambda: create(key="duplicate"))
        refused("conflict", lambda: create(updated_details))
        for invalid in (replace(details, description=" "), replace(details, low_stock_milli=1.5),
                        replace(details, purchase_cost_cents=True), replace(details, selling_price_cents=-1)):
            refused("validation", lambda: create(invalid, key="invalid"))
        refused("validation", lambda: edit(True))
        self.assertEqual([self.snapshot(f"SELECT * FROM {table}")
                          for table in ("inventory_items", "inventory_requests", "audit_events")], before)

        another = create(replace(details, sku="RC-2"), key="another")
        refused("duplicate_sku", lambda: edit(1, value=replace(updated_details, sku=another.sku)))
        updated = edit(1)
        self.assertEqual((updated.revision, updated.selling_price_cents, updated.bin), (2, 51, "B2"))
        refused("stale_revision", lambda: edit(1, key="stale"))
        refused("conflict", lambda: edit(2))
        with patch("repairlib.storage.repairs.audit", side_effect=sqlite3.IntegrityError("Synthetic failure")):
            with self.assertRaises(sqlite3.IntegrityError):
                archive(2)
        self.assertEqual(read(first.id), updated)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_requests"), [(4,)])
        archived = archive(2)
        self.assertEqual((archived.revision, archived.active, archived.updated_at),
                         (3, False, archived.archived_at))
        self.assertEqual(archive(2), archived)
        self.assertEqual(edit(1), updated)
        self.assertEqual(create(), first)
        self.assertEqual(read(first.id), archived)
        refused("archived", lambda: edit(3, key="after-archive"))
        refused("archived", lambda: archive(3, key="repeat-archive"))
        refused("duplicate_sku", lambda: create(key="reserved"))
        self.assertEqual(listing().items, [another])
        page = listing(include_archived=True, limit=1)
        self.assertEqual((page.items, page.next_before), ([another], another.id))
        self.assertEqual(listing(include_archived=True, before=page.next_before).items, [archived])
        self.assertEqual(self.snapshot(
            """SELECT shop_id,actor_user_id,actor_username,source,token_label,operation,entity_id
               FROM audit_events WHERE entity_type='inventory_item' ORDER BY id"""
        ), [(1, 1, "owner", "api", "Synthetic catalog", "inventory.create", first.id),
            (2, 4, "other", "api", "Synthetic catalog", "inventory.create", second.id),
            (1, 1, "owner", "api", "Synthetic catalog", "inventory.create", another.id),
            (1, 2, "tech", "api", "Synthetic catalog", "inventory.edit", first.id),
            (1, 2, "tech", "api", "Synthetic catalog", "inventory.archive", first.id)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_requests"), [(5,)])

    def test_schema_20_upgrade_preserves_receipts_and_restore_is_atomic(self):
        from repairlib.domain import inventory, stock

        initialization.initialize_database(self.path, "Prior shop", "UTC", "owner", "unused", target=20)
        access = self.token(1)
        details = inventory.ItemInput(sku="PRIOR", description="Archived fuse", unit="pcs")
        item = inventory.create_item(self.workspace, access, details, key="create")
        item = inventory.edit_item(self.workspace, access, item.id, replace(details, bin="A"),
                                   revision=1, key="edit")
        stock.receive_stock(self.workspace, access, item.id, 1250, revision=2, key="receive")
        archived = inventory.archive_item(self.workspace, access, item.id, revision=3, key="archive")
        receipts = self.snapshot(REQUESTS)
        ledger = stock.list_movements(self.workspace, access, item.id)
        initialization.upgrade_database(self.path)
        self.assertEqual(self.snapshot("PRAGMA user_version"), [(initialization.SCHEMA_VERSION,)])
        self.assertEqual(self.snapshot("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.snapshot(REQUESTS), receipts)
        self.assertEqual(inventory.archive_item(self.workspace, access, item.id, revision=3, key="archive"),
                         archived)

        def restore():
            return inventory.restore_item(self.workspace, access, item.id, revision=4, key="archive")

        with patch.object(inventory.repairs, "audit", side_effect=RuntimeError("Synthetic audit failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic audit failure"):
                restore()
        self.assertEqual(inventory.read_item(self.workspace, access, item.id), archived)
        self.assertEqual(self.snapshot(REQUESTS), receipts)
        restored = restore()
        self.assertEqual(restore(), restored)
        self.assertEqual(restored, replace(archived, revision=5, archived_at=None, updated_at=restored.updated_at))
        self.assertEqual(stock.list_movements(self.workspace, access, item.id), ledger)

    def test_populated_schema_16_upgrade_preserves_data_and_retries(self):
        initialization.initialize_database(self.path, "Prior shop", "UTC", "owner", "unused", target=16)
        access = self.token(1)
        details = CustomerInput("Synthetic prior customer")
        customer = create_customer(self.workspace, access, details, key="prior")
        history = self.snapshot("SELECT * FROM schema_migrations ORDER BY version")
        audits = self.snapshot("SELECT * FROM audit_events")
        apply = initialization.apply_migration

        def interrupted(db, version):
            apply(db, version)
            raise sqlite3.OperationalError("Synthetic interrupted migration")

        with patch.object(initialization, "apply_migration", interrupted):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(self.path)
        self.assertEqual(self.snapshot("PRAGMA user_version"), [(16,)])
        self.assertEqual(self.snapshot("SELECT name FROM sqlite_master WHERE name LIKE 'inventory_%'"), [])
        for _ in range(2):
            initialization.upgrade_database(self.path)
        self.assertEqual(initialization.schema_details(self.path), ("initialized", initialization.SCHEMA_VERSION))
        self.assertEqual(self.snapshot("SELECT * FROM schema_migrations WHERE version <= 16 ORDER BY version"),
                         history)
        self.assertEqual(self.snapshot("PRAGMA foreign_key_check"), [])
        self.assertEqual(read_customer(self.workspace, access, customer.id)[0], customer)
        self.assertEqual(create_customer(self.workspace, access, details, key="prior"), customer)
        self.assertEqual(self.snapshot("SELECT * FROM audit_events"), audits)
