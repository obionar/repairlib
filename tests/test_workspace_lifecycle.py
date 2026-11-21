"""Owner maintenance contracts on disposable local workspaces."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest

from repairlib.domain.sessions import login, principal_for_session
from repairlib.domain.setup import initialize_demo_workspace, initialize_workspace
from repairlib.lifecycle.workspace import database_path, maintenance_lock
from repairlib.storage.initialization import SCHEMA_VERSION, initialize_database
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_demo import PASSWORD


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace with spaces"

    def command(self, *args):
        return subprocess.run(
            [*COMMAND, *args, "--workspace", str(self.workspace)], input="", text=True,
            capture_output=True, timeout=15, env=python_env(),
        )

    def status(self):
        result = self.command("status", "--json")
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def demo(self):
        initialize_demo_workspace(self.workspace, username="owner", password=PASSWORD)

    def connect(self):
        return closing(sqlite3.connect(database_path(self.workspace)))

    def test_status_missing_old_and_live_local_counts(self):
        code, report = self.status()
        self.assertEqual((code, report["status"], report["ready"]), (0, "missing", False))
        self.assertIsNone(report["customers"])
        self.assertFalse(self.workspace.exists())

        path = database_path(self.workspace)
        path.parent.mkdir(parents=True)
        initialize_database(path, "Old", "UTC", "owner", "unused", target=1)
        before = path.read_bytes()
        code, report = self.status()
        self.assertNotEqual(code, 0)
        self.assertEqual((report["status"], report["schema_version"], report["ready"]),
                         ("upgrade_required", 1, False))
        self.assertEqual(path.read_bytes(), before)

        self.workspace = self.root / "live"
        self.demo()
        state = database_path(self.workspace).parent
        before = set(state.iterdir())
        code, report = self.status()
        self.assertEqual((code, report["customers"]), (0, 3))
        self.assertEqual(set(state.iterdir()), before)
        with maintenance_lock(self.workspace, exclusive=False), self.connect() as db:
            with db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other')")
                db.execute("INSERT INTO customers(shop_id, name) VALUES (2, 'Other customer')")
                db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'WAL customer')")
            code, report = self.status()
            self.assertEqual(code, 0)
            self.assertEqual((report["ready"], report["schema_version"], report["demo_seeded"]),
                             (True, SCHEMA_VERSION, True))
            self.assertEqual((report["customers"], report["repairs"]), (4, 0))
            self.assertNotIn("WAL customer", json.dumps(report))
            self.assertNotIn(PASSWORD, json.dumps(report))

    def test_cleanup_lock_staleness_and_preservation(self):
        self.demo()
        files = self.workspace / "state" / "files"
        staging = files / ".staging"
        staging.mkdir(parents=True)
        stale = staging / ("a" * 32 + ".part")
        fresh = staging / ("b" * 32 + ".part")
        originals = files / "shop-2" / ("c" * 32)
        recovery = self.workspace / ".setup-recovery" / "state" / "repairlib.sqlite3"
        backup = self.workspace / "backups" / "copy.part"
        config = self.workspace / "config.toml"
        cache = self.workspace / "cache" / "unknown.part"
        unknown = staging / "notes.part"
        linked = staging / ("d" * 32 + ".part")
        hardlink = staging / ("f" * 32 + ".part")
        db_path = database_path(self.workspace)
        preserved = (fresh, originals, recovery, backup, config, cache, unknown)
        for path in (stale, *preserved):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep original bytes")
        linked.symlink_to(originals)
        hardlink.hardlink_to(originals)
        old = time.time() - 7200
        for path in (stale, originals, unknown, backup, cache):
            os.utime(path, (old, old))
        before = db_path.read_bytes()
        with maintenance_lock(self.workspace, exclusive=False):
            locked = self.command("cleanup")
            self.assertNotEqual(locked.returncode, 0)
            self.assertIn("running", locked.stderr)
            self.assertTrue(stale.exists())
        cleaned = self.command("cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(linked.is_symlink())
        self.assertEqual(hardlink.read_bytes(), b"keep original bytes")
        self.assertEqual(db_path.read_bytes(), before)
        for path in preserved:
            self.assertEqual(path.read_bytes(), b"keep original bytes")

        staging.rename(files / "old-staging")
        staging.symlink_to(originals.parent, target_is_directory=True)
        self.assertNotEqual(self.command("cleanup").returncode, 0)
        self.assertEqual(originals.read_bytes(), b"keep original bytes")

    def test_confirmed_reset_preserves_credentials_and_is_atomic(self):
        self.demo()
        session = login(self.workspace, "owner", PASSWORD, now=2_000_000_000)
        self.assertIsNotNone(session)
        (self.workspace / "state" / "files" / ".staging").mkdir(parents=True)
        with self.connect() as db:
            with db:
                db.execute("""
                    INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key)
                    VALUES (1,1,1,'synthetic-token-hash','fixture','read','fixture')
                """)
            accounts = [db.execute(f"SELECT * FROM {table}").fetchall()
                        for table in ("users", "sessions", "api_tokens", "shop_settings")]
            customers = db.execute("SELECT * FROM customers ORDER BY id").fetchall()
            audit = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
            with db:
                db.execute("""
                    CREATE TRIGGER fail_demo_reset BEFORE INSERT ON audit_events
                    WHEN NEW.operation = 'demo.reset'
                    BEGIN SELECT RAISE(ABORT, 'synthetic injected failure'); END
                """)
        unconfirmed = self.command("demo", "reset")
        self.assertNotEqual(unconfirmed.returncode, 0)
        self.assertIn("--confirm", unconfirmed.stderr)
        with maintenance_lock(self.workspace, exclusive=False):
            locked = self.command("demo", "reset", "--confirm")
            self.assertNotEqual(locked.returncode, 0)
            self.assertIn("running", locked.stderr)
        failed = self.command("demo", "reset", "--confirm")
        self.assertNotEqual(failed.returncode, 0)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT * FROM customers ORDER BY id").fetchall(), customers)
            self.assertEqual(db.execute("SELECT * FROM audit_events ORDER BY id").fetchall(), audit)
            with db:
                db.execute("DROP TRIGGER fail_demo_reset")
        reset = self.command("demo", "reset", "--confirm")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        with self.connect() as db:
            self.assertEqual([db.execute(f"SELECT * FROM {table}").fetchall()
                              for table in ("users", "sessions", "api_tokens", "shop_settings")], accounts)
            restored = db.execute("SELECT * FROM customers ORDER BY id").fetchall()
            self.assertNotEqual(restored, customers)
            self.assertEqual([row[:8] for row in restored], [row[:8] for row in customers])
            self.assertEqual(db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()[:len(audit)], audit)
            self.assertEqual(db.execute("""
                SELECT actor_kind, actor_user_id, operation FROM audit_events ORDER BY id DESC LIMIT 1
            """).fetchone(), ("maintenance", None, "demo.reset"))
        repeat = self.command("demo", "reset", "--confirm")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(principal_for_session(self.workspace, session, now=2_000_000_001).username,
                         "owner")

    def test_reset_refuses_missing_business_changed_and_second_shop(self):
        self.assertNotEqual(self.command("demo", "reset", "--confirm").returncode, 0)
        self.assertFalse(self.workspace.exists())
        cases = (
            ("business", None, "seed marker"),
            ("edited", "UPDATE customers SET notes = 'changed' WHERE id = 1", "changed"),
            ("added", "INSERT INTO customers(shop_id, name) VALUES (1, 'Another')", "changed"),
            ("second-shop", "INSERT INTO shops(id, name) VALUES (2, 'Other')", "one shop"),
            ("attachment-bytes", None, "attachment"),
        )
        for name, mutation, reason in cases:
            with self.subTest(name=name):
                self.workspace = self.root / name
                if name == "business":
                    initialize_workspace(self.workspace, shop="Synthetic Repair Shop",
                                         timezone="UTC", username="owner", password=PASSWORD)
                else:
                    self.demo()
                if mutation:
                    with self.connect() as db, db:
                        db.execute(mutation)
                if name == "attachment-bytes":
                    blob = self.workspace / "state" / "files" / "shop-1" / ("e" * 32)
                    blob.parent.mkdir(parents=True)
                    blob.write_bytes(b"unrecognized original")
                before = database_path(self.workspace).read_bytes()
                refused = self.command("demo", "reset", "--confirm")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(reason, refused.stderr.lower())
                self.assertEqual(database_path(self.workspace).read_bytes(), before)
                if name == "attachment-bytes":
                    self.assertEqual(blob.read_bytes(), b"unrecognized original")
