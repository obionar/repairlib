"""Estimates are retired (owner, 2026-11-21): no tables, routes, commands or tools remain."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from repairlib.storage import initialization

ESTIMATE_TABLES = "SELECT name FROM sqlite_master WHERE name LIKE 'estimate%' ORDER BY name"


class NoEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "repairlib.sqlite3"

    def test_new_workspace_has_no_estimate_tables(self):
        initialization.initialize_database(self.path, "Shop", "UTC", "owner", "hash")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(ESTIMATE_TABLES).fetchall(), [])

    def test_upgrade_drops_estimates_and_keeps_repairs_and_charges(self):
        initialization.initialize_database(self.path, "Shop", "UTC", "owner", "hash",
                                           target=initialization.MIGRATIONS.index("0036_drop_estimates.sql"))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO repairs(shop_id, reported_issue) VALUES (1, 'Synthetic issue')")
            db.execute("INSERT INTO repair_charges(shop_id,repair_id,kind,description,quantity_milli,unit,"
                       "unit_price_cents,created_by,created_by_username) "
                       "VALUES (1,1,'labor','Bench labour',1000,'h',5000,1,'owner')")
            db.execute("INSERT INTO estimates(shop_id,repair_id,version,status,note,parts_subtotal_cents,"
                       "labor_subtotal_cents,subtotal_cents,discount_kind,discount_value,discount_cents,"
                       "total_cents,created_by,created_by_username) "
                       "VALUES (1,1,1,'draft','',0,5000,5000,'none',0,0,5000,1,'owner')")
            db.execute("INSERT INTO estimate_lines(shop_id,estimate_id,position,kind,description,"
                       "quantity_milli,unit,unit_price_cents,line_total_cents) "
                       "VALUES (1,1,1,'labor','Bench labour',1000,'h',5000,5000)")
        initialization.upgrade_database(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(ESTIMATE_TABLES).fetchall(), [])
            self.assertEqual(db.execute("SELECT description FROM repair_charges").fetchall(), [("Bench labour",)])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (initialization.SCHEMA_VERSION,))

    def test_no_adapter_mentions_estimates(self):
        source = Path(__file__).resolve().parents[1] / "src" / "repairlib"
        offenders = [str(path.relative_to(source)) for path in source.rglob("*")
                     if path.suffix in {".py", ".html"} and "estimate" in path.read_text(encoding="utf-8").lower()
                     and path.name not in {"portable_shop.py", "initialization.py"}]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
