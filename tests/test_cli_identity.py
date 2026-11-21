"""Local CLI identity, plus the disposable dev-listener helpers shared by adapter tests."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

import httpx
from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]


@contextmanager
def workspace_client(workspace):
    """An in-process API/web client on the workspace, yielded as `(client, None)`.

    Adapter tests use it to create data over HTTP while the CLI or MCP subprocess works on the same files.
    A real socket and server process are only needed where a test is about the listener (`running_listener`).
    """
    with TestClient(create_app(workspace=workspace), base_url="http://127.0.0.1",
                    follow_redirects=False) as client:
        yield client, None


@contextmanager
def running_listener(argv, port):
    server = subprocess.Popen(argv, env=python_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                          follow_redirects=False, timeout=0.5) as client:
            # Cold app construction exceeds eight seconds on the supported Pi.
            until = monotonic() + 30
            while monotonic() < until and server.poll() is None:
                try:
                    if client.get("/health").json().get("ready"):
                        break
                except httpx.TransportError:
                    sleep(0.05)
            else:
                raise AssertionError("Disposable HTTP server did not start")
            client.timeout = httpx.Timeout(5)
            yield client, server
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)


class CLIIdentityTests(unittest.TestCase):
    """The CLI and MCP run on the shop machine as built-in local actors, without tokens."""

    def run_cli(self, *args, code=0):
        result = subprocess.run([*COMMAND, *args], capture_output=True, text=True, timeout=10, env=python_env())
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def test_cli_and_mcp_take_a_workspace_not_credentials(self):
        for command in (("customers", "list"), ("repairs", "show"), ("mcp",), ("passwd",)):
            usage = self.run_cli(*command, "--help").stdout
            self.assertIn("--workspace", usage)
            self.assertNotIn("--profile", usage)
            self.assertNotIn("--token", usage)

    def test_uninitialized_workspace_is_refused_without_creating_files(self):
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "absent"
            refused = self.run_cli("customers", "list", "--workspace", str(missing), code=3)
            self.assertIn("not ready", refused.stderr)
            self.assertFalse(missing.exists())

    def test_cli_changes_are_audited_as_the_cli_actor(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            initialize_workspace(workspace, shop="Synthetic", timezone="UTC", username="owner", password=PASSWORD)
            self.run_cli("customers", "create", "--workspace", str(workspace), "--name", "Synthetic", "--key", "k1")
            with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
                rows = db.execute("SELECT actor_kind, actor_username, source, token_label FROM audit_events "
                                  "WHERE operation IN ('user.local_actor', 'customer.create') ORDER BY id").fetchall()
                users = db.execute("SELECT username, role, password_hash FROM users ORDER BY id").fetchall()
            self.assertEqual(rows, [("maintenance", None, "cli", None), ("user", "cli", "cli", None)])
            self.assertEqual([user[:2] for user in users], [("owner", "admin"), ("cli", "admin")])
            self.assertEqual(users[1][2], "!")
            self.run_cli("customers", "list", "--workspace", str(workspace))
            with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone(), (2,))

    def test_person_named_like_a_local_actor_blocks_the_cli_without_changes(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            initialize_workspace(workspace, shop="Synthetic", timezone="UTC", username="owner", password=PASSWORD)
            with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db, db:
                db.execute("UPDATE users SET username = 'cli'")  # an account from before the name was reserved
            with self.assertRaises(ValueError):
                initialize_workspace(Path(temporary) / "other", shop="Synthetic", timezone="UTC",
                                     username="mcp", password=PASSWORD)
            refused = self.run_cli("customers", "list", "--workspace", str(workspace), code=7)
            self.assertIn("Rename it first", refused.stderr)
            with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT username FROM users").fetchall(), [("cli",)])
