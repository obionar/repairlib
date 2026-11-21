"""Pre-migration backup guard: `migrate --backup ARCHIVE` refuses without a matching restore point."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest

from repairlib.lifecycle.workspace import database_path
from repairlib.storage.initialization import SCHEMA_VERSION, initialize_database
from test_baseline import python_env
from test_cli_identity import COMMAND


class MigrateBackupGuardTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        self.workspace = self.prior_workspace("prior", SCHEMA_VERSION - 1)
        self.database = database_path(self.workspace)
        self.archive = self.root / "prior.zip"
        self.run_cli("backup", "create", "--workspace", str(self.workspace),
                     "--output", str(self.archive), check=True)

    def prior_workspace(self, name, target):
        workspace = self.root / name
        workspace.mkdir(mode=0o700)
        (workspace / ".maintenance.lock").touch(mode=0o600)
        path = database_path(workspace)
        path.parent.mkdir(mode=0o700)
        initialize_database(path, "Prior", "UTC", "owner", "unused", target=target, demo=True)
        return workspace

    def run_cli(self, *args, check=False):
        result = subprocess.run([*COMMAND, *args], input="", text=True, capture_output=True,
                                timeout=60, env=python_env())
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def migrate(self, *flags):
        return self.run_cli("migrate", "--workspace", str(self.workspace), *flags)

    def version(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    def assert_refused(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.database.read_bytes(), self.before)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    def test_matching_archive_upgrades_and_is_kept_byte_for_byte(self):
        archived = self.archive.read_bytes()
        result = self.migrate("--backup", str(self.archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.archive), result.stdout)
        self.assertIn(f"schema {SCHEMA_VERSION - 1}", result.stdout)
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.archive.read_bytes(), archived)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["prior", "prior.zip"])

    def test_plain_migrate_still_upgrades_without_a_backup(self):
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_missing_archive_is_refused(self):
        self.before = self.database.read_bytes()
        self.assert_refused(self.migrate("--backup", str(self.root / "missing.zip")), "not found")

    def test_corrupt_or_edited_archive_is_refused(self):
        self.before = self.database.read_bytes()
        data = bytearray(self.archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.archive.write_bytes(bytes(data))
        edited = self.archive.read_bytes()
        result = self.migrate("--backup", str(self.archive))
        self.assert_refused(result, "Backup")
        self.assertEqual(self.archive.read_bytes(), edited)
        garbage = self.root / "garbage.zip"
        garbage.write_bytes(b"not a backup")
        garbage.chmod(0o600)
        self.assert_refused(self.migrate("--backup", str(garbage)), "standalone SQLite snapshot")

    def test_archive_of_a_different_schema_is_refused(self):
        current = self.prior_workspace("current", SCHEMA_VERSION)
        wrong = self.root / "current.zip"
        self.run_cli("backup", "create", "--workspace", str(current), "--output", str(wrong), check=True)
        self.before = self.database.read_bytes()
        archived = wrong.read_bytes()
        result = self.migrate("--backup", str(wrong))
        self.assert_refused(result, f"schema {SCHEMA_VERSION} does not match")
        self.assertEqual(wrong.read_bytes(), archived)

    def test_archive_from_another_workspace_at_same_schema_is_refused(self):
        other = self.prior_workspace("other", SCHEMA_VERSION - 1)
        with closing(sqlite3.connect(database_path(other))) as db, db:
            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Different workshop record')")
        wrong = self.root / "other.zip"
        self.run_cli("backup", "create", "--workspace", str(other), "--output", str(wrong), check=True)
        self.before = self.database.read_bytes()
        self.assert_refused(self.migrate("--backup", str(wrong)), "contents do not match")

    def test_newly_committed_wal_data_makes_same_workspace_backup_stale(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA journal_mode = WAL")
            with db:
                db.execute("INSERT INTO customers (shop_id, name, notes) VALUES (1, ?, ?)",
                           ("New workshop record", "not visible in the backup\0suffix"))
            self.before = self.database.read_bytes()
            wal = self.database.with_name(self.database.name + "-wal")
            before_wal = wal.read_bytes()
            self.assert_refused(self.migrate("--backup", str(self.archive)), "contents do not match")
            self.assertEqual(wal.read_bytes(), before_wal)

    def test_matching_database_snapshot_can_guard_a_workspace_without_attachments(self):
        snapshot = self.root / "before.sqlite3"
        self.run_cli("backup", "snapshot", "--workspace", str(self.workspace),
                     "--output", str(snapshot), check=True)
        result = self.migrate("--backup", str(snapshot))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_database_only_snapshot_cannot_guard_retained_attachments(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO repairs(shop_id, reported_issue) VALUES (1, 'Synthetic issue')")
            db.execute(
                "INSERT INTO attachments(shop_id, repair_id, uploader_id, uploader_username, filename, "
                "media_type, size_bytes, sha256, storage_name) "
                "VALUES (1, 1, 1, 'owner', 'lab.bin', 'application/octet-stream', 1, ?, ?)",
                ("a" * 64, "b" * 32),
            )
        snapshot = self.root / "before.sqlite3"
        self.run_cli("backup", "snapshot", "--workspace", str(self.workspace),
                     "--output", str(snapshot), check=True)
        self.before = self.database.read_bytes()
        self.assert_refused(self.migrate("--backup", str(snapshot)), "Use a full backup archive")

    def test_backup_inside_workspace_is_refused_without_writes(self):
        inside = self.workspace / self.archive.name
        self.archive.rename(inside)
        self.before = self.database.read_bytes()
        self.assert_refused(self.migrate("--backup", str(inside)), "must be outside")


if __name__ == "__main__":
    unittest.main()
