"""Explicit initialization gates using disposable workspaces."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_baseline import get_health, python_env

PASSWORD = "synthetic setup password"
CLI = [sys.executable, "-m", "repairlib"]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "shop workspace"
        self.db = self.workspace / "state" / "repairlib.sqlite3"
        self.command = [
            *CLI, "setup", "--workspace", str(self.workspace),
            "--shop", "Synthetic Shop", "--timezone", "Europe/Nicosia",
            "--username", "owner",
        ]

    def setup_cli(self, *, password=PASSWORD):
        return subprocess.run(
            self.command, input=password + "\n", capture_output=True,
            text=True, env=python_env(), timeout=10,
        )

    def setup_operation(self):
        from repairlib.domain.setup import initialize_workspace

        initialize_workspace(
            self.workspace, shop="Synthetic Shop", timezone="Europe/Nicosia",
            username="owner", password=PASSWORD,
        )

    def test_setup_persists_after_restart_and_keeps_secrets_out_of_argv(self):
        from argon2 import PasswordHasher

        help_result = subprocess.run(
            [*CLI, "setup", "--help"], capture_output=True, check=True,
            text=True, env=python_env(), timeout=10,
        )
        self.assertNotIn("--password", help_result.stdout)
        proc = self.setup_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(PASSWORD, proc.stdout + proc.stderr)
        doctor = subprocess.run(
            [*CLI, "doctor", "--workspace", str(self.workspace)], capture_output=True,
            text=True, env=python_env(), timeout=10,
        )
        self.assertIn("initialized", doctor.stdout)
        health = asyncio.run(get_health(self.workspace))
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["state"], "initialized")
        self.assertTrue(health.json()["ready"])
        self.assertTrue(self.db.is_file())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT name FROM shops").fetchall(), [("Synthetic Shop",)])
            self.assertEqual(db.execute("SELECT timezone FROM shop_settings").fetchall(),
                             [("Europe/Nicosia",)])
            name, role, hashed = db.execute("SELECT username, role, password_hash FROM users").fetchone()
            self.assertEqual((name, role), ("owner", "admin"))
            self.assertTrue(PasswordHasher().verify(hashed, PASSWORD))
            self.assertEqual(db.execute("SELECT actor_kind, actor_user_id, operation FROM audit_events")
                             .fetchall(), [("maintenance", None, "setup")])
        self.assertNotIn(PASSWORD.encode(), self.db.read_bytes())
        before = self.db.read_bytes()
        self.assertNotEqual(self.setup_cli().returncode, 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_invalid_setup_has_no_side_effects(self):
        proc = self.setup_cli(password="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.workspace.exists())

    def test_second_shop_cannot_supply_audit_actor(self):
        self.setup_operation()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            for table in ("users", "audit_events", "shop_settings"):
                columns = {row[1]: row[3] for row in db.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(columns["shop_id"], 1, table)
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other shop')")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO audit_events(shop_id, actor_kind, actor_user_id, operation) "
                    "VALUES (2, 'user', 1, 'test')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO shop_settings(shop_id, timezone) VALUES (3, 'UTC')")

    def test_initialization_failure_rolls_back_sql(self):
        from repairlib.storage.initialization import initialize_database

        staged = Path(self.temp.name) / "staged.sqlite3"
        with patch("repairlib.storage.initialization.seed_workspace", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                initialize_database(staged, "Shop", "UTC", "owner", "synthetic hash")
        with closing(sqlite3.connect(staged)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (0,))

    def test_concurrent_setup_publishes_one_workspace(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(2)

        def attempt():
            barrier.wait(timeout=5)
            return self.setup_cli()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(attempt)
            second = pool.submit(attempt)
            results = [first.result(timeout=12), second.result(timeout=12)]
        self.assertEqual(sum(p.returncode == 0 for p in results), 1)
        refused = next(p for p in results if p.returncode)
        self.assertRegex(refused.stderr, "already initialized|maintenance is in progress")
        self.assertTrue(self.db.is_file())

    def test_interrupted_publication_can_retry(self):
        crash = (
            "import os, sys; from pathlib import Path;"
            "from unittest.mock import patch;"
            "from repairlib.domain.setup import initialize_workspace;"
            "patch('repairlib.lifecycle.workspace.publish_state',"
            " side_effect=lambda *args: os._exit(71)).start();"
            "initialize_workspace(Path(sys.argv[1]), shop='Shop', timezone='UTC',"
            " username='owner', password=sys.stdin.readline().rstrip('\\n'));"
        )
        proc = subprocess.run(
            [sys.executable, "-c", crash, str(self.workspace)], input=PASSWORD + "\n",
            capture_output=True, text=True, env=python_env(), timeout=10,
        )
        self.assertEqual(proc.returncode, 71, proc.stderr)
        self.assertFalse(self.db.exists())
        from repairlib.doctor import diagnose_workspace

        self.assertEqual(diagnose_workspace(self.workspace).status, "interrupted")
        proc = self.setup_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.db.is_file())

    def test_newer_schema_is_refused_without_modification(self):
        from repairlib.doctor import diagnose_workspace

        self.setup_operation()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version = 999")
        before = self.db.read_bytes()
        self.assertEqual(diagnose_workspace(self.workspace).status, "unsupported_schema")
        health = asyncio.run(get_health(self.workspace))
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()["state"], "unsupported_schema")
        proc = self.setup_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("schema", proc.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_runtime_holds_shared_maintenance_lock_until_shutdown(self):
        from repairlib.app import create_app
        from repairlib.lifecycle.workspace import WorkspaceError, maintenance_lock

        self.setup_operation()
        app = create_app(workspace=self.workspace)

        async def verify_lifespan():
            async with app.router.lifespan_context(app):
                with maintenance_lock(self.workspace, exclusive=False):
                    with self.assertRaises(WorkspaceError):
                        with maintenance_lock(self.workspace, exclusive=True):
                            self.fail("Exclusive maintenance admitted during runtime")

        asyncio.run(verify_lifespan())
        with maintenance_lock(self.workspace, exclusive=True):
            health = asyncio.run(get_health(self.workspace))
            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json()["state"], "maintenance")
