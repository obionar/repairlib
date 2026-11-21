"""`profile check` reports the pre-write facts without writing or exposing secrets."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from repairlib.domain.setup import initialize_workspace
from repairlib.lifecycle.backup_schedule import scheduled_snapshot
from repairlib.storage.initialization import SCHEMA_VERSION
from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]
FIELDS = ("Workspace:", "State:", "Schema:", "Backup verified today:", "Identity:", "Role:", "Server:")


class ProfileCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        initialize_workspace(self.workspace, shop="Synthetic", timezone="UTC",
                             username="owner", password=PASSWORD)
        self.database = self.workspace / "state" / "repairlib.sqlite3"

    def run_check(self, *, code=0, env=None):
        result = subprocess.run(
            [*COMMAND, "profile", "check", "--workspace", str(self.workspace)],
            capture_output=True, text=True, timeout=30, env={**python_env(), **(env or {})})
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def test_healthy_profile_names_every_field_and_exits_zero(self):
        result = self.run_check()
        for field in FIELDS:
            self.assertIn(field, result.stdout)
        self.assertIn(f"Workspace: {self.workspace}", result.stdout)
        self.assertIn(f"Schema: {SCHEMA_VERSION} (expected {SCHEMA_VERSION})", result.stdout)
        self.assertIn("Backup verified today: no", result.stdout)
        self.assertIn("Identity: cli", result.stdout)
        self.assertIn("Role: admin", result.stdout)
        self.assertIn("Server: not configured", result.stdout)
        # Read-only: the built-in local actor is created by the first write, not by this check.
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE username = 'cli'").fetchone(), (0,))

    def test_verified_backup_today_is_reported(self):
        self.assertIsNotNone(scheduled_snapshot(self.workspace))
        self.assertIn("Backup verified today: yes", self.run_check().stdout)

    def test_outdated_schema_blocks_and_names_the_expected_version(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,))
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        result = self.run_check(code=1)
        self.assertIn(f"Schema: {SCHEMA_VERSION - 1} (expected {SCHEMA_VERSION})", result.stdout)
        self.assertIn("migrate", result.stderr)

    def test_unreachable_configured_server_blocks(self):
        result = self.run_check(code=1, env={"REPAIRLIB_BASE_URL": "http://127.0.0.1:1"})
        self.assertIn("Server: http://127.0.0.1:1", result.stdout)
        self.assertIn("unreachable", result.stderr)

    def test_no_secret_appears_in_the_output(self):
        result = self.run_check()
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        self.assertNotIn("argon2", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
