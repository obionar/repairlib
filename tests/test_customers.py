"""S1b shared customer creation and listing gates."""

from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import secrets
import sqlite3
import subprocess
from unittest.mock import patch
from unittest import TestCase
from uuid import uuid4

from argon2 import PasswordHasher
import httpx

from repairlib.domain.customers import create_customer
from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
import test_tokens
from test_tokens import ACCOUNT, PASSWORD, hidden, issued_secret

API = "/api/v1/customers"
FIELDS = {"name": "Synthetic <Customer>", "phone": "+357 555", "email": "demo@example.test",
          "address": "Example street", "notes": "Synthetic note"}


class CustomerTests(test_tokens.TokenFixture, TestCase):

    def test_browser_create_api_replay_and_scoped_restart_readback(self):
        with self.client() as client:
            self.assertEqual(client.get(API).status_code, 401)
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            form = client.get("/customers/new")
            self.assertEqual(form.status_code, 200)
            key = hidden(form, "key")
            body = {**FIELDS, "csrf": hidden(form, "csrf"), "key": key}
            self.assertEqual(client.post("/customers/new", data={**body, "name": ""}).status_code, 422)
            self.assertEqual(client.post("/customers/new", data={**body, "csrf": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/customers/new", data=body).status_code, 303)
            headers = {**auth, "Idempotency-Key": key}
            response = client.post(API, headers=headers, json=FIELDS)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["revision"], 1)
            self.assertEqual(client.post(API, headers=headers, json={**FIELDS, "name": "Mismatch"}).status_code, 409)
            self.assertEqual(client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={**FIELDS, "shop_id": 2}).status_code, 422)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
            db.execute("INSERT INTO customers(shop_id, name) VALUES (2,'Outside record')")
        with self.client() as client:
            results = client.get(API, headers=auth).json()
            self.assertEqual([item["name"] for item in results["items"]], [FIELDS["name"]])
            self.sign_in(client)
            page = client.get("/customers").text
            self.assertIn("Synthetic &lt;Customer&gt;", page)
            self.assertNotIn("Outside record", page)
        self.assertEqual(self.snapshot("SELECT source, actor_username FROM audit_events "
                                       "WHERE operation='customer.create'"), [("web", "owner")])

    def test_role_scope_and_api_pagination(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT 1,'viewer','viewer',password_hash FROM users WHERE id=1")
        with self.client() as client:
            self.sign_in(client, "viewer")
            viewer = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
            viewer_auth = {"Authorization": f"Bearer {viewer}", "Idempotency-Key": uuid4().hex}
            self.assertEqual(client.get("/customers/new").status_code, 403)
            self.assertEqual(client.post(API, headers=viewer_auth, json=FIELDS).status_code, 403)
            self.assertEqual(client.get(API, headers=viewer_auth).status_code, 200)
        with self.client() as client:
            self.sign_in(client)
            reader = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
            writer = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="write")))
            self.assertEqual(client.post(API, headers={"Authorization": f"Bearer {reader}",
                                                       "Idempotency-Key": uuid4().hex}, json=FIELDS).status_code, 403)
            auth = {"Authorization": f"Bearer {writer}"}
            for name in ("First", "Second"):
                self.assertEqual(client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                             json={"name": name}).status_code, 201)
            page = client.get(API, headers=auth, params={"limit": 1}).json()
            self.assertEqual([item["name"] for item in page["items"]], ["Second"])
            next_page = client.get(API, headers=auth, params={"limit": 1, "before": page["next_before"]}).json()
            self.assertEqual([item["name"] for item in next_page["items"]], ["First"])
            self.assertIsNone(next_page["next_before"])
            self.assertEqual(client.get(API, headers=auth, params={"limit": 0}).status_code, 422)
        self.assertEqual(self.snapshot("SELECT actor_username, token_label FROM audit_events "
                                       "WHERE operation='customer.create'"), [("owner", "Workshop CLI")] * 2)

    def test_real_cli_retry_and_list(self):
        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))

            def cli(*args, stdin=None):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout) if "--json" in args else None

            key = uuid4().hex
            created = cli("customers", "create", "--name", "CLI customer", "--key", key, "--json")
            self.assertEqual(cli("customers", "create", "--name", "CLI customer", "--key", key,
                                 "--json"), created)
            self.assertEqual(cli("customers", "list", "--json")["items"],
                             [{**created, "open_repair_count": 0}])

    def test_committed_create_can_be_retried_after_response_loss(self):
        result = None

        def commit_then_disconnect(*args, **kwargs):
            nonlocal result
            result = create_customer(*args, **kwargs)
            raise httpx.ReadError("Synthetic response loss")

        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex}
            with patch("repairlib.adapters.api.customers.create_customer", commit_then_disconnect):
                self.assertEqual(client.post(API, headers=headers, json=FIELDS).status_code, 500)
        with self.client() as client:
            retry = client.post(API, headers=headers, json=FIELDS)
            self.assertEqual(retry.json(), asdict(result))
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customers"), [(1,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='customer.create'"),
                         [(1,)])

    def test_populated_schema_three_recovery_and_upgrade(self):
        restored = self.workspace.parent / "restore"
        old = self.workspace.parent / "previous.sqlite3"
        initialization.initialize_database(old, "Previous shop", "UTC", "owner",
                                           PasswordHasher().hash(PASSWORD), target=3)
        bearer = "rl_" + secrets.token_urlsafe(32)
        (restored / "state").mkdir(parents=True)
        copy = restored / "state" / self.db.name
        with closing(sqlite3.connect(old)) as db, closing(sqlite3.connect(copy)) as backup:
            with db:
                db.execute(
                    "INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key) "
                    "VALUES (1,1,1,?,'Prior profile','write',?)",
                    (sha256(bearer.encode()).hexdigest(), uuid4().hex),
                )
            db.backup(backup)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))

        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic migration failure")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(copy)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (3,))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='customers'").fetchone())
        for _ in range(2):
            result = subprocess.run([*COMMAND, "migrate", "--workspace", str(restored)],
                                    env=python_env(), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        with workspace_client(restored) as (client, _):
            headers = {"Authorization": f"Bearer {bearer}", "Idempotency-Key": uuid4().hex}
            self.assertEqual(client.post(API, headers=headers, json=FIELDS).status_code, 201)
        with closing(sqlite3.connect(copy)) as db, closing(sqlite3.connect(old)) as prior:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT name FROM customers").fetchall(), [(FIELDS["name"],)])
            self.assertEqual(prior.execute("PRAGMA user_version").fetchone(), (3,))
