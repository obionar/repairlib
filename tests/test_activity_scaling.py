"""Repair activity semantics and query plans on an upgraded workspace."""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from repairlib.storage.initialization import SCHEMA_VERSION, initialize_database, migration_content, upgrade_database
from repairlib.storage.repairs import list_activity
from repairlib.storage.sessions import connect


ACTIVITY_INDEX = "audit_events_activity"


class ActivityScalingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "workspace.sqlite3"
        initialize_database(path, "Synthetic Shop", "UTC", "owner", "synthetic-password-hash", target=26)
        with closing(connect(path, "rw")) as db, db:
            self.seed_activity(db)
        upgrade_database(path)
        self.db = self.enterContext(closing(connect(path, "ro")))
        self.db.row_factory = sqlite3.Row

    def seed_activity(self, db):
        db.execute("INSERT INTO shops VALUES (2, 'Other shop')")
        db.execute("INSERT INTO customers(id, shop_id, name) VALUES (1, 1, 'Unrelated customer')")
        db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (1, 1, 'One'), (2, 1, 'Two')")
        for repair_id in (1, 2):
            db.execute("""INSERT INTO work_entries(id, shop_id, repair_id, author_id, author_username, kind, body)
                          VALUES (?, 1, ?, 1, 'owner', 'work', 'Bench note')""", (30 + repair_id, repair_id))
            db.execute("""INSERT INTO repair_charges(id, shop_id, repair_id, kind, description, quantity_milli,
                                                    unit, unit_price_cents, created_by, created_by_username)
                          VALUES (?, 1, ?, 'labor', 'Labor', 1000, 'hour', 100, 1, 'owner')""",
                       (40 + repair_id, repair_id))
            db.execute("""INSERT INTO attachments(id, shop_id, repair_id, uploader_id, uploader_username,
                                                  filename, media_type, size_bytes, sha256, storage_name)
                          VALUES (?, 1, ?, 1, 'owner', 'synthetic.bin', 'application/octet-stream', 1, ?, ?)""",
                       (60 + repair_id, repair_id, "0" * 64, f"{repair_id:032x}"))
        db.executemany("""INSERT INTO audit_events(id, shop_id, actor_kind, operation, entity_type, entity_id)
                          VALUES (?, ?, 'maintenance', ?, ?, ?)""", [
            (10, 1, "repair.create", "repair", 1),
            (11, 1, "repair.create", "repair", 2),
            (12, 1, "work_entry.create", "work_entry", 31),
            (13, 1, "work_entry.create", "work_entry", 32),
            (14, 1, "charge.create", "charge", 41),
            (15, 1, "charge.create", "charge", 42),
            (16, 1, "estimate.create", "estimate", 51),  # Retired estimates stay out of activity.
            (17, 1, "estimate.create", "estimate", 52),
            (18, 1, "attachment.create", "attachment", 61),
            (19, 1, "attachment.create", "attachment", 62),
            (20, 1, "repair.discount", "repair_discount", 1),
            (21, 1, "repair.discount", "repair_discount", 2),
            (22, 2, "repair.create", "repair", 1),
            (23, 2, "work_entry.create", "work_entry", 31),
            (24, 1, "customer.update", "customer", 1),
            (25, 1, "repair.stage", "repair", 1),
            (26, 1, "work_entry.create", "work_entry", 1),  # No matching work entry.
        ])

    def test_activity_is_scoped_and_pages_in_descending_id_order(self):
        for before, limit, expected in (
            (None, 20, [25, 20, 18, 14, 12, 10]),
            (None, 3, [25, 20, 18]),
            (18, 20, [14, 12, 10]),
            (19, 3, [18, 14, 12]),
            (10, 20, []),
        ):
            with self.subTest(before=before, limit=limit):
                rows = list_activity(self.db, 1, 1, before, limit)
                self.assertEqual([row.id for row in rows], expected)

    def activity_plan(self, before):
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            list_activity(self.db, 1, 1, before, 4)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)
        return [row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + statements[0])]

    def test_activity_plan_uses_index_without_scanning_audit_events(self):
        for before in (None, 18):
            with self.subTest(before=before):
                plan = "\n".join(self.activity_plan(before))
                self.assertNotIn("SCAN audit_events", plan, plan)
                self.assertIn(ACTIVITY_INDEX, plan, plan)

    def test_upgrade_applies_activity_index_migration(self):
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        recorded = self.db.execute("SELECT sha256 FROM schema_migrations WHERE version = 27").fetchone()
        self.assertEqual(recorded[0], sha256(migration_content(27)).hexdigest())
        self.assertIn(ACTIVITY_INDEX, [row[1] for row in self.db.execute("PRAGMA index_list('audit_events')")])
