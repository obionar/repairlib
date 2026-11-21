"""S1b token onboarding, isolation and replay gates."""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.domain.setup import initialize_workspace
from repairlib.storage import initialization
from support.workspace import clone_workspace
from test_baseline import python_env

ORIGIN = "http://127.0.0.1"
ACCOUNT = "/account/tokens"
IDENTITY = "/api/v1/identity"
PASSWORD = "synthetic token gate passphrase"


def hidden(response, name):
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    if not match:
        raise AssertionError(f"Missing {name} control")
    return match[1]


def issued_secret(response):
    match = re.search(r'id="token-secret"[^>]*>([A-Za-z0-9_-]+)<', response.text)
    if not match:
        raise AssertionError("No newly issued token was shown")
    return match[1]


class TokenFixture:
    reuse_workspace = True

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "workspace"
        self.db = self.workspace / "state" / "repairlib.sqlite3"
        if self.reuse_workspace:
            clone_workspace(self.workspace, PASSWORD)
            return
        initialize_workspace(self.workspace, shop="Synthetic", timezone="UTC",
                             username="owner", password=PASSWORD)

    def client(self):
        return TestClient(create_app(workspace=self.workspace), base_url=ORIGIN,
                          follow_redirects=False)

    def sign_in(self, client, username="owner"):
        form = client.get("/login")
        reply = client.post("/login", data={
            "username": username, "password": PASSWORD, "csrf": hidden(form, "csrf"),
        })
        self.assertEqual(reply.status_code, 303)

    def issue_form(self, client, **overrides):
        form = client.get(ACCOUNT)
        self.assertEqual(form.status_code, 200)
        return {"csrf": hidden(form, "csrf"), "key": hidden(form, "key"),
                "name": "Workshop CLI", "scope": "write", "password": PASSWORD, **overrides}

    def snapshot(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql, parameters).fetchall()


class TokenTests(TokenFixture, unittest.TestCase):
    def test_issuance_replay_redaction_restart_and_revocation(self):
        with self.client() as client:
            unauth = client.get(IDENTITY)
            self.assertEqual(unauth.status_code, 401)
            self.assertEqual(unauth.json()["error"]["code"], "unauthenticated")
            self.sign_in(client)
            self.assertEqual(client.get(IDENTITY).status_code, 401)
            form = self.issue_form(client, name="CLI <Workshop>")
            reply = client.post(ACCOUNT, data=form)
            self.assertEqual(reply.status_code, 201)
            token = issued_secret(reply)
            self.assertEqual(reply.headers["cache-control"], "no-store")
            self.assertTrue(PASSWORD not in reply.text)
            self.assertTrue(token not in client.get(ACCOUNT).text)
            replay = client.post(ACCOUNT, data=form)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(token not in replay.text)
            self.assertEqual(client.post(ACCOUNT, data={**form, "scope": "read"}).status_code, 409)
            self.assertEqual(client.post(ACCOUNT, data={**form, "password": "wrong"}).status_code, 403)
            (token_id, digest), = self.snapshot("SELECT id, token_hash FROM api_tokens")
            self.assertTrue(digest == sha256(token.encode()).hexdigest())
            self.assertEqual(self.snapshot(
                "SELECT actor_username, source, entity_id FROM audit_events WHERE operation='token.create'"
            ), [("owner", "web", token_id)])
            auth = {"Authorization": f"Bearer {token}"}
            self.assertEqual(client.get(IDENTITY, headers=auth).json(), {
                "shop_id": 1, "user_id": 1, "username": "owner", "role": "admin",
                "scope": "write", "token_name": "CLI <Workshop>",
            })
            self.assertTrue(IDENTITY in client.get("/api/v1/openapi.json").json()["paths"])
        with self.client() as client:
            self.assertEqual(client.get(IDENTITY, headers=auth).status_code, 200)
            self.sign_in(client)
            revoke = f"{ACCOUNT}/{token_id}/revoke"
            page = client.get(revoke)
            fields = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                      "revision": hidden(page, "revision"), "password": PASSWORD}
            self.assertEqual(client.post(revoke, data={**fields, "revision": "2"}).status_code, 409)
            self.assertEqual(client.post(revoke, data=fields).status_code, 303)
            self.assertEqual(client.post(revoke, data=fields).status_code, 303)
            self.assertEqual(client.get(IDENTITY, headers=auth).status_code, 401)
        self.assertEqual(self.snapshot("SELECT operation FROM audit_events WHERE entity_id = ? ORDER BY id",
                                       (token_id,)), [("token.create",), ("token.revoke",)])

    def test_role_owner_shop_and_csrf_refusals(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other shop')")
            db.execute(
                "INSERT INTO users(id, shop_id, username, role, password_hash) "
                "SELECT 2, 1, 'viewer', 'viewer', password_hash FROM users WHERE id=1"
            )
            db.execute(
                "INSERT INTO users(id, shop_id, username, role, password_hash) "
                "SELECT 3, 2, 'other', 'admin', password_hash FROM users WHERE id=1"
            )
        with self.client() as client:
            self.sign_in(client, "viewer")
            form = self.issue_form(client)
            self.assertEqual(client.post(ACCOUNT, data=form).status_code, 403)
            read_form = {**form, "scope": "read"}
            self.assertEqual(client.post(ACCOUNT, data={**read_form, "csrf": "invalid"}).status_code, 403)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM api_tokens"), [(0,)])
            reply = client.post(ACCOUNT, data=read_form)
            self.assertEqual(reply.status_code, 201)
            viewer_token = issued_secret(reply)
            auth = {"Authorization": f"Bearer {viewer_token}"}
            self.assertEqual(client.get(IDENTITY, headers=auth).json()["scope"], "read")
        outside = "rl_" + secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, scope, create_key) "
                    "VALUES (2, 1, 1, ?, 'Mismatched owner', 'read', ?)",
                    (sha256(outside.encode()).hexdigest(), uuid4().hex),
                )
            db.execute(
                "INSERT INTO api_tokens(id, shop_id, user_id, user_revision, token_hash, name, scope, create_key) "
                "VALUES (2, 2, 3, 1, ?, 'Outside', 'read', ?)",
                (sha256(outside.encode()).hexdigest(), uuid4().hex),
            )
            db.execute("UPDATE users SET revision = revision + 1 WHERE id=2")
        with self.client() as client:
            self.sign_in(client)
            self.assertNotIn("Outside", client.get(ACCOUNT).text)
            self.assertEqual(client.get(f"{ACCOUNT}/1/revoke").status_code, 404)
            self.assertEqual(client.get(f"{ACCOUNT}/2/revoke").status_code, 404)
            self.assertEqual(client.get(IDENTITY, headers={"Authorization": f"Bearer {outside}"}).status_code, 401)
            self.assertEqual(client.get(IDENTITY, headers=auth).status_code, 401)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation LIKE 'token.%'"), [(1,)])

    def test_token_issuance_rechecks_the_verified_account(self):
        verify = PasswordHasher.verify

        def change_account(hasher, hashed, password):
            verified = verify(hasher, hashed, password)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE users SET revision = revision + 1")
            return verified

        with self.client() as client:
            self.sign_in(client)
            form = self.issue_form(client)
            with patch.object(PasswordHasher, "verify", change_account):
                self.assertEqual(client.post(ACCOUNT, data=form).status_code, 403)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM api_tokens"), [(0,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation LIKE 'token.%'"), [(0,)])

    def test_populated_schema_two_backup_upgrade_and_transactional_failure(self):
        prior = self.workspace.parent / "prior"
        (prior / "state").mkdir(parents=True)
        (prior / ".maintenance.lock").touch()
        old = prior / "state" / self.db.name
        initialization.initialize_database(old, "Prior shop", "UTC", "prior",
                                           PasswordHasher().hash(PASSWORD), target=2)
        # A schema-2 database has no last_seen_at column, so seed the session row
        # directly; the upgrade migration is what gives it an idle clock.
        active_session = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("INSERT INTO sessions(token_hash, shop_id, user_id, user_revision, expires_at) "
                       "SELECT ?, shop_id, id, revision, ? FROM users WHERE username = 'prior'",
                       (sha256(active_session.encode()).hexdigest(), 2_000_000_000 + 43_200))

        restored = self.workspace.parent / "restored"
        (restored / "state").mkdir(parents=True)
        (restored / ".maintenance.lock").touch()
        copy = restored / "state" / self.db.name
        with closing(sqlite3.connect(old)) as source, closing(sqlite3.connect(copy)) as backup:
            source.backup(backup)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(backup.execute("PRAGMA foreign_key_check").fetchall(), [])

        apply = initialization.apply_migration

        def interrupt(db, version):
            apply(db, version)
            raise sqlite3.OperationalError("synthetic migration failure")

        with patch.object(initialization, "apply_migration", interrupt):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(copy)
        self.assertEqual(initialization.schema_status(copy), "upgrade_required")
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (2,))
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='api_tokens'").fetchall(), [])
        for _ in range(2):
            result = subprocess.run([sys.executable, "-m", "repairlib", "migrate", "--workspace", str(restored)],
                                    capture_output=True, text=True, env=python_env(), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        with TestClient(create_app(workspace=restored), base_url=ORIGIN) as client:
            client.cookies.set("repairlib_session", active_session)
            self.assertEqual(client.get(ACCOUNT).status_code, 200)
        with closing(sqlite3.connect(old)) as source, closing(sqlite3.connect(copy)) as db:
            self.assertEqual(source.execute("PRAGMA user_version").fetchone(), (2,))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (initialization.SCHEMA_VERSION,))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT name FROM shops").fetchall(), [("Prior shop",)])
