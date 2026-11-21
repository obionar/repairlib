"""Offline database recovery gates on populated disposable workspaces."""

from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
from textwrap import dedent
from time import monotonic, time
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

from argon2 import PasswordHasher

from repairlib.__main__ import main
from repairlib.contracts import AttachmentDetach, CustomerInput, SettingsPatch
from repairlib.domain import attachments, customers, repairs, sessions, settings, tokens
from repairlib.domain.setup import initialize_demo_workspace
from repairlib.lifecycle import backup_archive
from repairlib.lifecycle.workspace import database_path, maintenance_lock, workspace_status
from repairlib.storage.initialization import SCHEMA_VERSION, initialize_database
from repairlib.storage.recovery import SnapshotError, online_wait
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_demo import PASSWORD


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "synthetic workspace"
        self.database = database_path(self.workspace)
        self.output = self.root / "database snapshot.sqlite3"
        initialize_demo_workspace(self.workspace, username="owner", password=PASSWORD)

    def snapshot_args(self, output=None, *, online=False):
        return ("backup", "snapshot", "--workspace", str(self.workspace),
                "--output", str(output or self.output), *(["--online"] if online else []))

    def command(self, *args):
        return subprocess.run(
            [*COMMAND, *args], text=True, capture_output=True, timeout=15, env=python_env(),
        )

    def assert_refusal(self, result, reason):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(reason, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(PASSWORD, result.stderr)


class DatabaseRecoveryTests(RecoveryFixture):
    def test_snapshot_includes_committed_wal_and_honors_maintenance_lock(self):
        with closing(sqlite3.connect(self.database)) as source:
            source.execute("PRAGMA wal_autocheckpoint = 0")
            with source:
                source.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Committed in WAL')")
            wal = self.database.with_name(self.database.name + "-wal")
            self.assertGreater(wal.stat().st_size, 32)
            source.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Uncommitted')")
            with maintenance_lock(self.workspace, exclusive=False):
                self.assert_refusal(self.command(*self.snapshot_args()), "running")
                self.assertFalse(self.output.exists())
            result = self.command(*self.snapshot_args())
            self.assertEqual(result.returncode, 0, result.stderr)
            source.rollback()
            expected = source.execute("SELECT * FROM customers ORDER BY id").fetchall()
            audit = source.execute("SELECT * FROM audit_events ORDER BY id").fetchall()

        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        before = self.output.read_bytes()
        verified = self.command("backup", "verify", str(self.output))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn(f"schema {SCHEMA_VERSION}", verified.stdout)
        with closing(sqlite3.connect(self.output.as_uri() + "?mode=ro", uri=True)) as copy:
            self.assertEqual(copy.execute("SELECT * FROM customers ORDER BY id").fetchall(), expected)
            self.assertEqual(copy.execute("SELECT * FROM audit_events ORDER BY id").fetchall(), audit)
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.output})
        self.assertEqual(len(expected), 4)

    def test_online_snapshot_copies_committed_wal_and_skips_uncommitted_rows(self):
        with closing(sqlite3.connect(self.database)) as source:
            source.execute("PRAGMA wal_autocheckpoint = 0")
            with source:
                source.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Committed online')")
            self.assertGreater(
                self.database.with_name(self.database.name + "-wal").stat().st_size, 32)
            source.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Uncommitted online')")
            with maintenance_lock(self.workspace, exclusive=False):
                result = self.command(*self.snapshot_args(online=True))
            self.assertEqual(result.returncode, 0, result.stderr)
            source.rollback()
            expected = source.execute("SELECT name FROM customers ORDER BY id").fetchall()

        verified = self.command("backup", "verify", str(self.output))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        with closing(sqlite3.connect(self.output.as_uri() + "?mode=ro", uri=True)) as copy:
            self.assertEqual(copy.execute("SELECT name FROM customers ORDER BY id").fetchall(), expected)
            self.assertIn(("Committed online",), expected)
            self.assertNotIn(("Uncommitted online",), expected)
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertFalse(os.path.lexists(self.output.with_name(self.output.name + "-wal")))
        self.assertFalse(os.path.lexists(self.output.with_name(self.output.name + "-shm")))
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.output})
        self.assert_refusal(self.command(*self.snapshot_args(online=True)), "exists")
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.output})

    def test_online_snapshot_uses_sqlite_backup_instead_of_a_file_copy(self):
        with patch("repairlib.lifecycle.recovery.copy_database",
                   side_effect=AssertionError("file copy")):
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(self.snapshot_args(online=True)), 0, stderr.getvalue())
        self.assertIn("Verified SQLite snapshot saved", stdout.getvalue())
        self.assertEqual(self.command("backup", "verify", str(self.output)).returncode, 0)

    def test_online_snapshot_refuses_maintenance_lock_without_partial_output(self):
        with maintenance_lock(self.workspace, exclusive=True):
            self.assert_refusal(self.command(*self.snapshot_args(online=True)), "maintenance")
            self.assertFalse(self.output.exists())
        self.assertEqual(set(self.root.iterdir()), {self.workspace})

    def test_online_snapshot_bounds_the_wait_for_a_busy_database(self):
        online_wait(monotonic() + 60)(sqlite3.SQLITE_BUSY, 0, 0)
        online_wait(monotonic() - 1)(sqlite3.SQLITE_OK, 0, 0)
        with self.assertRaises(SnapshotError):
            online_wait(monotonic() - 1)(sqlite3.SQLITE_BUSY, 0, 0)
        with self.assertRaises(SnapshotError):
            online_wait(monotonic() - 1)(sqlite3.SQLITE_LOCKED, 0, 0)

    def test_verify_refuses_corruption_integrity_foreign_keys_and_incompatible_schema(self):
        result = self.command(*self.snapshot_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        original = self.output.read_bytes()
        cases = (
            ("integrity", "PRAGMA ignore_check_constraints = ON",
             "UPDATE customers SET revision = 0 WHERE id = 1"),
            ("foreign key", "PRAGMA foreign_keys = OFF",
             "UPDATE customers SET shop_id = 999 WHERE id = 1"),
            ("schema", "UPDATE schema_migrations SET sha256 = 'invalid' WHERE version = 1"),
            ("schema", f"PRAGMA user_version = {SCHEMA_VERSION + 1}"),
        )
        for reason, *statements in cases:
            with self.subTest(statements=statements):
                self.output.write_bytes(original)
                with closing(sqlite3.connect(self.output)) as db, db:
                    for statement in statements:
                        db.execute(statement)
                before = self.output.read_bytes()
                self.assert_refusal(self.command("backup", "verify", str(self.output)), reason)
                self.assertEqual(self.output.read_bytes(), before)

        self.output.write_bytes(b"not a SQLite database")
        self.assert_refusal(self.command("backup", "verify", str(self.output)), "SQLite")
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.output})

    def test_failed_snapshot_preserves_source_and_refuses_unsafe_publication(self):
        self.output.write_bytes(b"keep existing")
        self.assert_refusal(self.command(*self.snapshot_args()), "exists")
        self.assertEqual(self.output.read_bytes(), b"keep existing")
        self.output.unlink()
        self.output.symlink_to(self.root / "missing-target")
        self.assert_refusal(self.command(*self.snapshot_args()), "exists")
        self.assertTrue(self.output.is_symlink())
        self.output.unlink()

        inside = self.database.parent / "unsafe.sqlite3"
        self.assert_refusal(self.command(*self.snapshot_args(inside)), "outside")
        self.assertFalse(inside.exists())
        public = self.root / "public"
        public.mkdir()
        public.chmod(0o755)
        self.assert_refusal(self.command(*self.snapshot_args(public / self.output.name)), "private")
        self.assertEqual(list(public.iterdir()), [])
        public.rmdir()

        before = self.database.read_bytes()
        stdout, stderr = StringIO(), StringIO()
        with patch("os.link", side_effect=OSError("synthetic publication failure")) as publish:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(self.snapshot_args()), 1)
        publish.assert_called_once()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("publish", stderr.getvalue())
        self.assertNotIn("synthetic publication failure", stderr.getvalue())
        self.assertEqual(set(self.root.iterdir()), {self.workspace})
        self.assertEqual(self.database.read_bytes(), before)

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("UPDATE customers SET shop_id = 999 WHERE id = 1")
        before = self.database.read_bytes()
        self.assert_refusal(self.command(*self.snapshot_args()), "foreign key")
        self.assertEqual(set(self.root.iterdir()), {self.workspace})
        self.assertEqual(self.database.read_bytes(), before)

    def test_snapshot_accepts_populated_prior_schema_without_migrating(self):
        previous = self.root / "previous"
        previous.mkdir(mode=0o700)
        (previous / ".maintenance.lock").touch(mode=0o600)
        path = database_path(previous)
        path.parent.mkdir(mode=0o700)
        initialize_database(path, "Prior", "UTC", "owner", "synthetic", target=SCHEMA_VERSION - 1,
                            demo=True)
        before = path.read_bytes()
        result = self.command("backup", "snapshot", "--workspace", str(previous),
                              "--output", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.command("backup", "verify", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"schema {SCHEMA_VERSION - 1}", result.stdout)
        with closing(sqlite3.connect(self.output)) as copy:
            self.assertEqual(copy.execute("SELECT count(*) FROM customers").fetchone(), (3,))
        self.assertEqual(path.read_bytes(), before)


class ArchiveRecoveryTests(RecoveryFixture):
    """Archive gates reuse the same disposable fixture and CLI helpers."""

    def setUp(self):
        super().setUp()
        self.archive = self.root / "backup.zip"
        self.archive_args = ("backup", "create", "--workspace", str(self.workspace),
                             "--output", str(self.archive))
        self.blobs = {}
        directory = self.database.parent / "files" / "shop-1"
        directory.mkdir(mode=0o700, parents=True)
        directory.parent.chmod(0o700)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO repairs(shop_id, customer_id, reported_issue) "
                       "VALUES (1, 1, 'Synthetic fault')")
            for index, suffix in enumerate(("", "detached", "purged"), 1):
                name = str(index) * 32
                content = bytes((0, 255, index)) + b"Original bytes"
                stamp = "2026-01-01T00:00:00.000Z"
                db.execute(
                    """INSERT INTO attachments
                        (shop_id, repair_id, uploader_id, uploader_username, filename, media_type,
                         size_bytes, sha256, storage_name, detached_at, purged_at, purged_by_username)
                    VALUES (1, 1, 1, 'owner', ?, 'application/octet-stream', ?, ?, ?, ?, ?, ?)""",
                    (suffix or "active", len(content), sha256(content).hexdigest(), name,
                     stamp if suffix else None, stamp if suffix == "purged" else None,
                     "owner" if suffix == "purged" else None),
                )
                # A stale purged blob may still be on disk after an interrupted unlink.
                blob = directory / name
                blob.touch(mode=0o600)
                blob.write_bytes(content)
                if suffix != "purged":
                    self.blobs[str(blob.relative_to(self.workspace))] = content

    def test_doctor_reports_attachment_damage_without_changing_workspace(self):
        args = ("doctor", "--workspace", str(self.workspace))
        directory = self.database.parent / "files" / "shop-1"
        purged = directory / ("3" * 32)
        purged.unlink()
        clean = self.command(*args)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("SQLite integrity: ok", clean.stdout)
        self.assertIn("SQLite foreign keys: ok", clean.stdout)
        self.assertIn("Retained attachments checked: 2", clean.stdout)

        active, detached = (self.workspace / path for path in self.blobs)
        active.unlink()
        detached.write_bytes(bytes(detached.stat().st_size))
        purged.write_bytes(b"obsolete original")
        staging = directory.parent / ".staging"
        staging.mkdir()
        (staging / "private-name.part").write_bytes(b"private staging bytes")
        (self.workspace / ".setup-leftover").mkdir()
        (self.root / ".restore-leftover").mkdir()
        before = self.tree_state(self.root)
        damaged = self.command(*args)
        self.assertNotEqual(damaged.returncode, 0)
        for line in ("Missing attachment IDs: 1", "SHA-256 mismatch attachment IDs: 2",
                     "Orphan entries: 1", "Upload staging entries: 1",
                     "Setup staging entries: 1", "Restore staging entries in workspace parent: 1"):
            self.assertIn(line, damaged.stdout)
        self.assertNotIn("private", damaged.stdout + damaged.stderr)
        self.assertEqual(self.tree_state(self.root), before)

        detached.write_bytes(b"short")
        resized = self.command(*args)
        self.assertNotEqual(resized.returncode, 0)
        self.assertIn("Size mismatch attachment IDs: 2", resized.stdout)

        detached.unlink()
        detached.symlink_to(purged)
        unsafe = self.command(*args)
        self.assertNotEqual(unsafe.returncode, 0)
        self.assertIn("Unsafe or unreadable attachment IDs: 2", unsafe.stdout)

    def test_doctor_checks_committed_wal_without_creating_sidecars(self):
        args = ("doctor", "--workspace", str(self.workspace))
        with maintenance_lock(self.workspace, exclusive=False):
            before = self.tree_state(self.workspace)
            busy = self.command(*args)
            self.assertNotEqual(busy.returncode, 0)
            self.assertIn("maintenance", busy.stdout)
            self.assertIn("SQLite integrity: unchecked", busy.stdout)
            self.assertEqual(self.tree_state(self.workspace), before)

        crashed = self.interrupt("""
            import os, sqlite3, sys
            db = sqlite3.connect(sys.argv[1])
            db.execute("PRAGMA wal_autocheckpoint = 0")
            db.execute("PRAGMA ignore_check_constraints = ON")
            db.execute("UPDATE customers SET revision = 0, shop_id = 999 WHERE id = 1")
            db.commit()
            os._exit(73)
        """, self.database)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        wal = self.database.with_name(self.database.name + "-wal")
        self.assertGreater(wal.stat().st_size, 32)
        self.database.with_name(self.database.name + "-shm").unlink()
        before = self.tree_state(self.workspace)
        damaged = self.command(*args)
        self.assertNotEqual(damaged.returncode, 0)
        self.assertIn("SQLite integrity: failed", damaged.stdout)
        self.assertIn("SQLite foreign keys: failed", damaged.stdout)
        self.assertEqual(damaged.stderr, "")
        self.assertEqual(self.tree_state(self.workspace), before)

    def test_rehearse_prior_schema_restore_and_interrupted_migration(self):
        prior = self.root / "prior"
        path = database_path(prior)
        prior.mkdir(mode=0o700)
        path.parent.mkdir(mode=0o700)
        (prior / ".maintenance.lock").touch(mode=0o600)
        initialize_database(path, "Prior shop", "UTC", "owner", PasswordHasher().hash(PASSWORD),
                            target=14)
        now = int(time())
        # A prior-schema session has no last_seen_at column yet, so seed the same
        # rows in the schema-14 shape instead of signing in against an old database.
        session_token = secrets.token_urlsafe(32)
        api_token = "rl" + "_" + secrets.token_urlsafe(32)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO sessions(token_hash, shop_id, user_id, user_revision, expires_at) "
                       "SELECT ?, shop_id, id, revision, ? FROM users WHERE username = 'owner'",
                       (sha256(session_token.encode()).hexdigest(), now + sessions.SESSION_SECONDS))
            db.execute("INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, scope, "
                       "create_key) SELECT shop_id, id, revision, ?, 'Recovery reader', 'write', 'fixture' "
                       "FROM users WHERE username = 'owner'", (sha256(api_token.encode()).hexdigest(),))
        access = customers.CustomerAccess("api", api_token)
        details = CustomerInput("Recovery customer")
        customer = customers.create_customer(prior, access, details, key="saved-request")
        content = b"prior original"
        name = "a" * 32
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO repairs(shop_id,customer_id,reported_issue) "
                       "VALUES (1,?,'Prior repair')", (customer.id,))
            db.execute("INSERT INTO work_entries(shop_id,repair_id,author_id,author_username,kind,body) "
                       "VALUES (1,1,1,'owner','note','Prior entry')")
            db.execute("INSERT INTO attachments(shop_id,repair_id,entry_id,uploader_id,uploader_username,"
                       "filename,media_type,size_bytes,sha256,storage_name) VALUES "
                       "(1,1,1,1,'owner','prior.bin','application/octet-stream',?,?,?)",
                       (len(content), sha256(content).hexdigest(), name))
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Other synthetic shop')")
            db.execute("INSERT INTO customers(id,shop_id,name) VALUES (99,2,'Other customer')")
            history = db.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            audit = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        original = path.parent / "files" / "shop-1" / name
        original.parent.mkdir(mode=0o700, parents=True)
        original.touch(mode=0o600)
        original.write_bytes(content)
        source_before = self.tree_state(prior)

        for args in (("backup", "create", "--workspace", str(prior), "--output", str(self.archive)),
                     ("backup", "verify", str(self.archive))):
            result = self.command(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("schema 14", result.stdout)
        archive_before = self.archive.read_bytes()

        destination = self.root / "restored prior"
        restore = self.restore_args(destination)
        stopped = self.interrupt("""
            import os, sys
            from repairlib.__main__ import main
            from repairlib.lifecycle import backup_archive
            backup_archive.publish_restore = lambda *args: os._exit(73)
            sys.exit(main(sys.argv[1:]))
        """, *restore)
        self.assertEqual(stopped.returncode, 73, stopped.stderr)
        self.assertFalse(destination.exists())
        staging = list(self.root.glob(".restore-*"))
        self.assertEqual(len(staging), 1)
        pending = self.command("doctor", "--workspace", str(destination))
        self.assertNotEqual(pending.returncode, 0)
        self.assertIn("Restore staging entries in workspace parent: 1", pending.stdout)
        staging_before = self.tree_state(staging[0])
        restored = self.command(*restore)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("schema 14", restored.stdout)
        prior_diagnostic = self.command("doctor", "--workspace", str(destination))
        self.assertIn("upgrade_required", prior_diagnostic.stdout)
        self.assertIn("Retained attachments checked: 1", prior_diagnostic.stdout)
        self.assertEqual(self.tree_state(staging[0]), staging_before)

        migrate = ("migrate", "--workspace", str(destination))
        interrupted = self.interrupt("""
            import os, sys
            from repairlib.__main__ import main
            from repairlib.storage import initialization
            apply = initialization.apply_migration
            def stop(db, version):
                db.execute("PRAGMA cache_size = 1")
                apply(db, version)
                os._exit(73)
            initialization.apply_migration = stop
            sys.exit(main(sys.argv[1:]))
        """, *migrate)
        self.assertEqual(interrupted.returncode, 73, interrupted.stderr)
        self.assertEqual(workspace_status(destination), "upgrade_required")
        before_retry = self.tree_state(destination)
        pending = self.command("doctor", "--workspace", str(destination))
        self.assertNotEqual(pending.returncode, 0)
        self.assertIn("Rollback journal present: yes", pending.stdout)
        self.assertIn("SQLite integrity: unchecked", pending.stdout)
        self.assert_refusal(self.command(*restore), "exists")
        self.assert_refusal(self.command("backup", "snapshot", "--workspace",
                                         str(destination), "--output", str(self.output)), "rollback journal")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.tree_state(destination), before_retry)
        upgraded = self.command(*migrate)
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        status = self.command("status", "--workspace", str(destination), "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)["ready"])
        migrated = self.tree_state(destination)
        rerun = self.command(*migrate)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(self.tree_state(destination), migrated)
        with maintenance_lock(destination, exclusive=False):
            self.assertEqual(customers.read_customer(destination, access, customer.id)[0], customer)
            self.assertEqual(customers.create_customer(destination, access, details, key="saved-request"), customer)
            with self.assertRaises(customers.CustomerError) as other:
                customers.read_customer(destination, access, 99)
            self.assertEqual(other.exception.status, 404)
            self.assertEqual(repairs.read_repair(destination, access, 1)[0].reported_issue, "Prior repair")
            item, blob = attachments.attachment_content(destination, access, 1, 1, inline=False)
            self.assertEqual((item.entry_ids, item.caption), ([1], ""))
            self.assertEqual(blob.read_bytes(), content)
        with closing(sqlite3.connect(database_path(destination))) as db:
            self.assertEqual(db.execute("SELECT * FROM audit_events ORDER BY id").fetchall(), audit)
            self.assertEqual(db.execute("SELECT * FROM schema_migrations WHERE version <= 14 "
                                        "ORDER BY version").fetchall(), history)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            # The restore kept the session and the upgrade gave it an idle clock.
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM sessions WHERE last_seen_at >= ?", (now,)).fetchone(), (1,))
        self.assertEqual(self.tree_state(prior), source_before)
        self.assertEqual(self.archive.read_bytes(), archive_before)

    def tree_state(self, root):
        return {
            p.relative_to(root): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                  os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in root.rglob("*")
        }

    def interrupt(self, script, *args):
        return subprocess.run(
            [sys.executable, "-B", "-c", dedent(script), *map(str, args)],
            text=True, capture_output=True, timeout=15, env=python_env(),
        )

    def create_archive(self):
        created = self.command(*self.archive_args)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn(f"archive saved (schema {SCHEMA_VERSION})", created.stdout)

    def test_archive_preserves_originals_and_recovery_metadata_under_maintenance_lock(self):
        with maintenance_lock(self.workspace, exclusive=False):
            self.assert_refusal(self.command(*self.archive_args), "running")
        self.assertFalse(self.archive.exists())

        self.create_archive()
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        original = self.archive.read_bytes()
        verified = self.command("backup", "verify", str(self.archive))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn(f"archive verified (schema {SCHEMA_VERSION})", verified.stdout)
        self.assertEqual(self.archive.read_bytes(), original)

        db_member = "state/repairlib.sqlite3"
        with ZipFile(self.archive) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "repairlib-backup-1")
            self.assertEqual(manifest["schema"], SCHEMA_VERSION)
            self.assertCountEqual(archive.namelist(), ["manifest.json", db_member, *self.blobs])
            self.assertEqual(set(manifest["files"]), {db_member, *self.blobs})
            for name, metadata in manifest["files"].items():
                data = archive.read(name)
                self.assertEqual(metadata, {"size": len(data), "sha256": sha256(data).hexdigest()})
                if name in self.blobs:
                    self.assertEqual(data, self.blobs[name])
            self.output.write_bytes(archive.read(db_member))

        with closing(sqlite3.connect(self.output)) as db:
            self.assertEqual(db.execute(
                "SELECT detached_at IS NOT NULL, purged_at IS NOT NULL FROM attachments ORDER BY id"
            ).fetchall(), [(0, 0), (1, 0), (1, 1)])
            self.assertEqual(db.execute("SELECT shop_id FROM local_workspace").fetchall(), [(1,)])
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone(), (1,))

    def test_verify_refuses_corrupt_archives_without_mutating_them(self):
        self.create_archive()
        with ZipFile(self.archive) as archive:
            members = {info.filename: (info, archive.read(info)) for info in archive.infolist()}
        manifest_info, encoded_manifest = members["manifest.json"]
        blob_name = next(iter(self.blobs))
        blob_info, blob_data = members[blob_name]
        extra = f"state/files/shop-1/{'3' * 32}"  # purged attachment

        def check(reason, *, replacements=(), removed=(), append=(), edit_manifest=None):
            path = self.root / "corrupt.zip"
            path.touch(mode=0o600, exist_ok=False)
            payloads = {key: item for key, item in members.items() if key not in removed}
            payloads.update(replacements)
            if edit_manifest is not None:
                manifest = json.loads(encoded_manifest)
                edit_manifest(manifest)
                payloads["manifest.json"] = (manifest_info, json.dumps(manifest).encode())
            with ZipFile(path, "w") as archive:
                for info, data in [*payloads.values(), *append]:
                    archive.writestr(info, data)
            before = path.read_bytes()
            self.assert_refusal(self.command("backup", "verify", str(path)), reason)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive, path})
            path.unlink()

        check("manifest", replacements=[("manifest.json", (manifest_info, b"{"))])
        check("schema", edit_manifest=lambda m: m.update(schema=SCHEMA_VERSION - 1))
        check("size", edit_manifest=lambda m: m["files"][blob_name].update(size=len(blob_data) + 1))
        check("limit", edit_manifest=lambda m: m["files"][blob_name].update(size=1 << 40))
        wrong = bytes(len(blob_data))
        check("hash", replacements=[(blob_name, (blob_info, wrong))])
        check("attachment", replacements=[(blob_name, (blob_info, wrong))],
              edit_manifest=lambda m: m["files"][blob_name].update(sha256=sha256(wrong).hexdigest()))
        check("membership", removed=[blob_name])
        check("attachment", removed=[blob_name], edit_manifest=lambda m: m["files"].pop(blob_name))

        for name, mode, reason in (
            ("../escape", stat.S_IFREG, "path"),
            (extra, stat.S_IFLNK, "entry"),
            (extra, stat.S_IFREG, "attachment"),
        ):
            info = ZipInfo(name)
            info.external_attr = (mode | 0o600) << 16
            check(reason, append=[(info, blob_data)], edit_manifest=lambda m: m["files"].update(
                {name: {"size": len(blob_data), "sha256": sha256(blob_data).hexdigest()}}))
        duplicate = ZipInfo(blob_name)
        duplicate.external_attr = (stat.S_IFREG | 0o600) << 16
        with self.assertWarns(UserWarning):
            check("duplicate", append=[(duplicate, blob_data)])

        db_name = "state/repairlib.sqlite3"
        db_info, db_bytes = members[db_name]
        for statement, reason in (
            ("UPDATE schema_migrations SET sha256 = 'invalid' WHERE version = 1", "migration history"),
            ("DROP INDEX repairs_queue", "schema differs"),
        ):
            self.output.write_bytes(db_bytes)
            with closing(sqlite3.connect(self.output)) as db, db:
                db.execute(statement)
            damaged = self.output.read_bytes()
            self.output.unlink()
            check(reason, replacements=[(db_name, (db_info, damaged))],
                  edit_manifest=lambda m: m["files"][db_name].update(
                      size=len(damaged), sha256=sha256(damaged).hexdigest()))

    def test_failed_archive_creation_preserves_source_and_existing_destination(self):
        self.archive.write_bytes(b"keep existing")
        self.assert_refusal(self.command(*self.archive_args), "exists")
        self.assertEqual(self.archive.read_bytes(), b"keep existing")
        self.archive.unlink()

        database_before = self.database.read_bytes()
        path, data = next(iter(self.blobs.items()))
        blob = self.workspace / path
        blob.unlink()
        self.assert_refusal(self.command(*self.archive_args), "attachment")
        blob.symlink_to(self.database)
        self.assert_refusal(self.command(*self.archive_args), "attachment")
        blob.unlink()
        blob.touch(mode=0o600)
        blob.write_bytes(bytes(len(data)))
        self.assert_refusal(self.command(*self.archive_args), "attachment hash")
        blob.write_bytes(data)

        stdout, stderr = StringIO(), StringIO()
        with patch("os.link", side_effect=OSError("synthetic publication failure")) as publish:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(self.archive_args), 1)
        publish.assert_called_once()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("publish", stderr.getvalue())
        self.assertEqual(set(self.root.iterdir()), {self.workspace})
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(blob.read_bytes(), data)

    def restore_args(self, destination):
        return ("backup", "restore", str(self.archive), "--workspace", str(destination))

    def test_restore_populated_workspace_and_read_back_through_domain(self):
        now = int(time())
        session = sessions.login(self.workspace, "owner", PASSWORD, now=now)
        web = settings.SettingsAccess("web", session)
        token = tokens.create_token(self.workspace, session, password=PASSWORD, name="Recovery reader",
                                    scope="write", key=uuid4().hex, now=now)
        api = customers.CustomerAccess("api", token)
        shop = settings.update_settings(self.workspace, web, SettingsPatch(1, name="Recovery shop"),
                                        key="recovery-settings")
        repair = repairs.read_repair(self.workspace, api, 1)[0]
        customer = customers.read_customer(self.workspace, api, repair.customer_id)[0]
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other synthetic shop')")
            db.execute("INSERT INTO customers(id, shop_id, name) VALUES (99, 2, 'Other customer')")
        self.create_archive()

        destination = self.root / "restored workspace"
        result = self.command(*self.restore_args(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"schema {SCHEMA_VERSION}", result.stdout)
        database = database_path(destination)
        status = self.command("status", "--workspace", str(destination), "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)["ready"])
        snapshot = self.command("backup", "snapshot", "--workspace", str(destination),
                                "--output", str(self.output))
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        verified = self.command("backup", "verify", str(self.output))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        with ZipFile(self.archive) as archive:
            manifest = json.loads(archive.read("manifest.json"))["files"]
            for name in self.blobs:
                data = (destination / name).read_bytes()
                record = manifest[name]
                self.assertEqual(len(data), record["size"])
                self.assertEqual(sha256(data).hexdigest(), record["sha256"])

        self.output.unlink()
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        for path in destination.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(set(destination.iterdir()), {database.parent, destination / ".maintenance.lock"})
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive, destination})

        with maintenance_lock(destination, exclusive=False):
            self.assertEqual(settings.read_settings(destination, web), shop)
            self.assertEqual(repairs.read_repair(destination, api, repair.id)[0], repair)
            self.assertEqual(customers.read_customer(destination, api, customer.id)[0], customer)
            with self.assertRaises(customers.CustomerError) as other_shop:
                customers.read_customer(destination, api, 99)
            self.assertEqual(other_shop.exception.status, 404)
            self.assertIsNotNone(sessions.login(destination, "owner", PASSWORD, now=int(time())))
            active, detached, purged = attachments.list_attachments(destination, api, repair.id)[0].items[::-1]
            self.assertFalse(active.detached)
            self.assertTrue(detached.detached)
            self.assertTrue(purged.purged)
            for item in (active, detached):
                _, path = attachments.attachment_content(destination, api, repair.id, item.id, inline=False)
                self.assertEqual(path.read_bytes(), self.blobs[str(path.relative_to(destination))])
            self.assertFalse((destination / "state" / "files" / "shop-1" / ("3" * 32)).exists())
            with self.assertRaises(repairs.RepairError) as gone:
                attachments.attachment_content(destination, api, repair.id, purged.id, inline=False)
            self.assertEqual(gone.exception.status, 410)
            attachments.change_detached(destination, api, repair.id, detached.id,
                                        AttachmentDetach(detached.revision, False), key="reattach-recovery")
        with maintenance_lock(destination, exclusive=False):
            self.assertFalse(attachments.read_attachment(destination, api, repair.id, detached.id).detached)

    def test_restore_refuses_existing_linked_and_unsafe_paths(self):
        self.create_archive()
        occupied = self.root / "occupied"
        occupied.mkdir()
        self.assert_refusal(self.command(*self.restore_args(occupied)), "exists")
        marker = occupied / "keep"
        marker.write_bytes(b"keep")
        self.assert_refusal(self.command(*self.restore_args(occupied)), "exists")
        linked = self.root / "linked"
        linked.symlink_to(self.root / "absent")
        self.assert_refusal(self.command(*self.restore_args(linked)), "exists")
        linked.unlink()
        linked.symlink_to(occupied, target_is_directory=True)
        self.assert_refusal(self.command(*self.restore_args(linked / "new")), "parent")
        self.assert_refusal(self.command(*self.restore_args(occupied / "new")), "private")
        with maintenance_lock(self.workspace, exclusive=False):
            self.assert_refusal(self.command(*self.restore_args(self.workspace)), "exists")
        self.assertEqual(list(occupied.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertTrue(linked.is_symlink())

    def test_restore_refuses_corrupt_payload_before_publication(self):
        self.create_archive()
        with ZipFile(self.archive) as archive:
            members = [(info, archive.read(info)) for info in archive.infolist()]
        corrupt_name = next(reversed(self.blobs))
        with ZipFile(self.archive, "w") as archive:
            for info, data in members:
                archive.writestr(info, bytes(len(data)) if info.filename == corrupt_name else data)
        before = self.archive.read_bytes()
        destination = self.root / "new"
        self.assert_refusal(self.command(*self.restore_args(destination)), "attachment hash")
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive})
        self.assertEqual(self.archive.read_bytes(), before)

    def test_restore_failed_interrupted_and_late_publication(self):
        self.create_archive()
        destination = self.root / "new"
        args = self.restore_args(destination)
        with patch.object(backup_archive, "publish_restore",
                          side_effect=OSError("synthetic publication failure")) as publish:
            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main(args), 1)
        publish.assert_called_once()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("publish", stderr.getvalue())
        self.assertNotIn("synthetic publication failure", stderr.getvalue())
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive})

        with patch.object(backup_archive, "publish_restore", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main(args)
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive})

        publish = backup_archive.publish_restore

        def late_arrival(*call_args):
            destination.mkdir()
            publish(*call_args)

        with patch.object(backup_archive, "publish_restore", side_effect=late_arrival):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main(args), 1)
        self.assertIn("exists", stderr.getvalue())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive, destination})

        destination.rmdir()

        def interrupted_after_commit(*call_args):
            publish(*call_args)
            raise KeyboardInterrupt

        with patch.object(backup_archive, "publish_restore", side_effect=interrupted_after_commit):
            with self.assertRaises(KeyboardInterrupt):
                main(args)
        with maintenance_lock(destination, exclusive=False):
            self.assertEqual(workspace_status(destination), "initialized")
            for path, data in self.blobs.items():
                self.assertEqual((destination / path).read_bytes(), data)
        self.assertEqual(set(self.root.iterdir()), {self.workspace, self.archive, destination})
