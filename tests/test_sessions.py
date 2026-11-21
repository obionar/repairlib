"""Authentication upgrade and credential checks."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_baseline import python_env


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.database = self.workspace / "state" / "repairlib.sqlite3"
        self.password = "synthetic passphrase"
        from repairlib.domain.setup import initialize_workspace

        initialize_workspace(
            self.workspace, shop="Example Shop", timezone="UTC",
            username="admin", password=self.password,
        )

    def test_upgrade_populated_workspace_and_read_only_startup(self):
        import asyncio

        from argon2 import PasswordHasher
        from repairlib.doctor import diagnose_workspace
        from repairlib.storage.initialization import initialize_database
        from test_baseline import get_health

        previous = self.workspace.parent / "previous"
        (previous / "state").mkdir(parents=True)
        (previous / ".maintenance.lock").touch()
        db_path = previous / "state" / "repairlib.sqlite3"
        initialize_database(db_path, "Prior shop", "UTC", "prior", PasswordHasher().hash(self.password),
                            target=1)
        self.assertEqual(diagnose_workspace(previous).status, "upgrade_required")
        self.assertEqual(asyncio.run(get_health(previous)).status_code, 503)
        self.assertEqual(list((previous / "state").iterdir()), [db_path])
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-m", "repairlib", "migrate", "--workspace", str(previous)],
                capture_output=True, text=True, env=python_env(), timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        before = sorted((p.name, p.read_bytes()) for p in db_path.parent.iterdir())
        self.assertEqual(diagnose_workspace(previous).status, "initialized")
        self.assertEqual(asyncio.run(get_health(previous)).status_code, 200)
        self.assertEqual(before, sorted((p.name, p.read_bytes()) for p in db_path.parent.iterdir()))
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            self.assertEqual(db.execute("SELECT name FROM shops").fetchall(), [("Prior shop",)])
            self.assertEqual(db.execute("SELECT username FROM users").fetchall(), [("prior",)])
            self.assertEqual(db.execute("SELECT operation FROM audit_events").fetchall(), [("setup",)])

    def test_sessions_are_hashed_expire_and_revoke(self):
        from repairlib.domain.sessions import login, principal_for_session, logout

        token = login(self.workspace, "admin", self.password, now=100)
        self.assertIsNotNone(token)
        with closing(sqlite3.connect(self.database)) as db:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            self.assertEqual(db.execute("SELECT token_hash FROM sessions").fetchone(), (digest,))
        principal = principal_for_session(self.workspace, token, now=101)
        self.assertEqual((principal.username, principal.role), ("admin", "admin"))
        self.assertIsNone(principal_for_session(self.workspace, token, now=100_000))
        logout(self.workspace, token, now=101)
        self.assertIsNone(principal_for_session(self.workspace, token, now=101))

    def test_invalid_prior_workspace_rolls_back_migration(self):
        from repairlib.doctor import diagnose_workspace
        from repairlib.storage.initialization import initialize_database

        previous = self.workspace.parent / "invalid prior"
        (previous / "state").mkdir(parents=True)
        (previous / ".maintenance.lock").touch()
        db_path = previous / "state" / "repairlib.sqlite3"
        initialize_database(db_path, "Prior shop", "UTC", "prior", "unused hash", target=1)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("INSERT INTO shops(name) VALUES ('Unexpected second shop')")
        proc = subprocess.run(
            [sys.executable, "-m", "repairlib", "migrate", "--workspace", str(previous)],
            capture_output=True, text=True, env=python_env(), timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(diagnose_workspace(previous).status, "upgrade_required")
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM shops").fetchone(), (2,))
            self.assertEqual(db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('local_workspace', 'sessions')"
            ).fetchall(), [])

    def test_second_shop_and_disabled_user_cannot_log_in(self):
        from repairlib.domain.sessions import login, principal_for_session

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other shop')")
            db.execute(
                "INSERT INTO users(shop_id, username, role, password_hash) "
                "SELECT 2, 'other', role, password_hash FROM users WHERE username = 'admin'"
            )
        self.assertIsNone(login(self.workspace, "other", self.password, now=100))
        self.assertIsNone(login(self.workspace, "admin", "wrong", now=100))
        token = login(self.workspace, "admin", self.password, now=100)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE users SET active = 0, revision = revision + 1 WHERE shop_id = 1")
        self.assertIsNone(principal_for_session(self.workspace, token, now=101))
        self.assertIsNone(login(self.workspace, "admin", self.password, now=101))

    def test_logout_refuses_a_session_after_its_account_revision_changes(self):
        from repairlib.domain.sessions import login, logout

        token = login(self.workspace, "admin", self.password, now=100)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE users SET revision = revision + 1")
        logout(self.workspace, token, now=101)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT operation FROM audit_events ORDER BY id").fetchall(),
                             [("setup",), ("login",)])

    def test_credential_change_during_password_verification_refuses_login(self):
        from argon2 import PasswordHasher
        from repairlib.domain.sessions import login

        verify = PasswordHasher.verify

        def disable_after_verify(hasher, hashed, password):
            answer = verify(hasher, hashed, password)
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE users SET active = 0, revision = revision + 1")
            return answer

        with patch.object(PasswordHasher, "verify", disable_after_verify):
            self.assertIsNone(login(self.workspace, "admin", self.password, now=100))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT operation FROM audit_events").fetchall(), [("setup",)])
