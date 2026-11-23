"""Fresh replay receipts and stock proof survive stopped-workspace cleanup (audit R01, R02)."""

from contextlib import closing
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from repairlib.client import local
from repairlib.contracts import PrinterChange
from repairlib.doctor import diagnose_workspace
from repairlib.domain import printing
from repairlib.lifecycle.operations import RECEIPT_TABLES, cleanup_workspace, prune_workspace_records
from test_inventory_adapters import ITEM
from test_repairs import DEVICE
from test_tokens import TokenFixture

FAR_FUTURE = int(time.time()) + 200 * 24 * 60 * 60  # Well beyond the 90-day receipt window.


class ReceiptRetentionTests(TokenFixture, unittest.TestCase):
    def rows(self, sql):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql).fetchall()

    def test_fresh_customer_receipt_survives_cleanup(self):
        ctx = local.open_workspace(self.workspace)
        first = local.create_customer(ctx, {"name": "Synthetic"}, "retry").id
        cleanup_workspace(self.workspace)
        ctx = local.open_workspace(self.workspace)
        self.assertEqual(local.create_customer(ctx, {"name": "Synthetic"}, "retry").id, first)
        self.assertEqual(self.rows("SELECT count(*) FROM customers"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM audit_events WHERE operation = 'customer.create'"), [(1,)])

    def test_every_receipt_insert_site_is_dated(self):
        ctx = local.open_workspace(self.workspace)
        local.create_customer(ctx, {"name": "Synthetic"}, "customer")
        repair = local.create_repair(ctx, DEVICE, "repair").id
        item = local.create_inventory_item(ctx, ITEM, "item")
        local.change_stock(ctx, item.id, "receive", {"quantity_milli": 2000, "revision": 1}, "receive")
        local.consume_part(ctx, repair, {"repair_revision": 1, "item_id": item.id, "item_revision": 2,
                                         "consumed_quantity_milli": 1000, "billed_quantity_milli": 1000}, "consume")
        for table in RECEIPT_TABLES:
            self.assertEqual(self.rows(f"SELECT count(*) FROM {table} WHERE created_at = ''"), [(0,)], table)

    def test_acknowledged_label_is_not_printed_again_after_cleanup(self):
        ctx = local.open_workspace(self.workspace)
        repair = local.create_repair(ctx, DEVICE, "repair").id
        with patch.object(printing, "p15_binary", return_value="/not-executed"), \
             patch.object(printing.subprocess, "run", return_value=Mock(returncode=0)) as driver:
            revision = printing.read_printer(ctx.workspace, ctx.access).revision
            config = printing.configure_printer(ctx.workspace, ctx.access,
                                                PrinterChange(driver="p15", revision=revision), key="config")

            def label():
                printing.print_p15(ctx.workspace, ctx.access, printer_revision=config.revision, key="label",
                                   repair_id=repair, repair_revision=local.show_repair(ctx, repair).revision)

            label()
            cleanup_workspace(self.workspace)
            ctx = local.open_workspace(self.workspace)
            label()
            self.assertEqual(driver.call_count, 1)

    def test_blank_dated_legacy_receipt_is_kept_and_old_dated_one_expires(self):
        ctx = local.open_workspace(self.workspace)
        local.create_customer(ctx, {"name": "Synthetic"}, "legacy")
        local.create_customer(ctx, {"name": "Other"}, "old")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE customer_requests SET created_at = '' WHERE request_key = 'legacy'")
            db.execute("UPDATE customer_requests SET created_at = '2000-01-01T00:00:00.000Z' "
                       "WHERE request_key = 'old'")
        prune_workspace_records(self.db, FAR_FUTURE)
        self.assertEqual(self.rows("SELECT request_key FROM customer_requests"), [("legacy",)])


class StockProofRetentionTests(TokenFixture, unittest.TestCase):
    def prune_beyond_window(self, workspace):
        prune_workspace_records(workspace / "state" / "repairlib.sqlite3", FAR_FUTURE)

    def stock_story(self, ctx):
        repair = local.create_repair(ctx, DEVICE, "stock-repair").id
        item = local.create_inventory_item(ctx, ITEM, "item")
        local.change_stock(ctx, item.id, "receive", {"quantity_milli": 2000, "revision": 1}, "receive")
        local.consume_part(ctx, repair, {"repair_revision": 1, "item_id": item.id, "item_revision": 2,
                                         "consumed_quantity_milli": 1000, "billed_quantity_milli": 1000}, "consume")
        def revisions():
            return {"repair_revision": local.show_repair(ctx, repair).revision,
                    "item_revision": local.show_inventory_item(ctx, item.id).revision}

        charge = local.consume_part(ctx, repair, {**revisions(), "item_id": item.id, "consumed_quantity_milli": 500,
                                                  "billed_quantity_milli": 500}, "consume-2")
        local.return_part(ctx, repair, charge.id, {**revisions(), "quantity_milli": 500, "reason": "Unused"},
                          "return")
        return item.id

    def test_native_stock_proof_survives_receipt_pruning_beyond_the_window(self):
        ctx = local.open_workspace(self.workspace)
        self.stock_story(ctx)
        self.assertEqual(diagnose_workspace(self.workspace).exit_status, 0)
        self.prune_beyond_window(self.workspace)
        self.assertEqual([i.code for i in local.reconcile_stock(ctx).issues], [])
        self.assertEqual(diagnose_workspace(self.workspace).exit_status, 0)

    def test_imported_stock_boundary_survives_receipt_pruning_beyond_the_window(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory
        source = local.open_workspace(self.workspace)
        self.stock_story(source)
        buffer = BytesIO()
        size = local.download_portable_shop(source, buffer)
        buffer.seek(0)
        with TemporaryDirectory() as temp:
            from pathlib import Path
            from test_tokens import PASSWORD
            from repairlib.domain.setup import initialize_workspace
            dest_path = Path(temp) / "workspace"
            initialize_workspace(dest_path, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
            dest = local.open_workspace(dest_path)
            local.import_portable_shop(dest, buffer, size, destination_shop_id=1, revision=1, key="import")
            self.assertEqual(diagnose_workspace(dest_path).exit_status, 0)
            self.prune_beyond_window(dest_path)
            self.assertEqual([i.code for i in local.reconcile_stock(dest).issues], [])
            self.assertEqual(diagnose_workspace(dest_path).exit_status, 0)


if __name__ == "__main__":
    unittest.main()
