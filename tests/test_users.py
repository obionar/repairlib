"""S1c same-shop user administration and offline recovery gates."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.domain.sessions import login, principal_for_session
from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_tokens import ACCOUNT, PASSWORD, TokenFixture, hidden, issued_secret

USERS = "/users"
API = "/api/v1/users"


def create_form(client, **overrides):
    page = client.get(f"{USERS}/new")
    if page.status_code != 200:
        return page
    fields = {
        "csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
        "username": "technician", "role": "technician", "password": "tech passphrase",
        **overrides,
    }
    return fields


def edit_form(client, user_id, **overrides):
    page = client.get(f"{USERS}/{user_id}/edit")
    if page.status_code != 200:
        return page
    selected = re.search(r'<option value="([^"]+)" selected>', page.text)
    if not selected:
        raise AssertionError("Missing selected role")
    return {
        "csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
        "revision": hidden(page, "revision"), "username": hidden(page, "username"),
        "role": selected[1], "active": "1", "password": "", **overrides,
    }


class UserTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def sign_in_password(self, client, username, password):
        page = client.get("/login")
        reply = client.post("/login", data={
            "username": username, "password": password, "csrf": hidden(page, "csrf"),
        })
        self.assertEqual(reply.status_code, 303)

    def test_browser_api_replay_password_role_and_disable_revocation(self):
        with self.client() as owner, self.client() as technician:
            token, auth = self.admin_auth(owner)
            self.assertNotIn(PASSWORD, owner.get(USERS).text)
            form = create_form(owner)
            self.assertEqual(owner.post(f"{USERS}/new", data=form).status_code, 303)
            self.assertEqual(owner.post(f"{USERS}/new", data=form).status_code, 303)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM users WHERE password_hash <> '!'"), [(2,)])
            listing = owner.get(API, headers=auth)
            self.assertEqual(listing.status_code, 200)
            record = next(item for item in listing.json()["items"] if item["username"] == "technician")
            self.assertEqual(set(record), {"id", "username", "role", "active", "revision"})
            self.assertEqual(record["revision"], 1)
            self.assertNotIn("tech passphrase", owner.get(USERS).text)

            self.sign_in_password(technician, "technician", "tech passphrase")
            technician_token = issued_secret(technician.post(
                ACCOUNT, data={"csrf": hidden(technician.get(ACCOUNT), "csrf"),
                               "key": uuid4().hex, "name": "Technician CLI", "scope": "write",
                               "password": "tech passphrase"},
            ))
            technician_auth = {"Authorization": f"Bearer {technician_token}"}
            self.assertEqual(technician.get("/api/v1/identity", headers=technician_auth).status_code, 200)

            changed = edit_form(owner, record["id"], role="viewer")
            self.assertEqual(owner.post(f"{USERS}/{record['id']}/edit", data=changed).status_code, 303)
            self.assertEqual(technician.get("/customers").status_code, 303)
            self.assertEqual(technician.get("/api/v1/identity", headers=technician_auth).status_code, 401)
            password_session = login(self.workspace, "technician", "tech passphrase", now=2_000_000_000)
            self.assertIsNotNone(password_session)
            self.assertEqual(owner.get(f"{USERS}/{record['id']}/edit").status_code, 200)

            key = uuid4().hex
            url = f"{API}/{record['id']}"
            password_change = {"revision": 2, "password": "replacement passphrase"}
            response = owner.patch(url, headers={**auth, "Idempotency-Key": key}, json=password_change)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["revision"], 3)
            self.assertIsNone(principal_for_session(self.workspace, password_session, now=2_000_000_001))
            replay = owner.patch(url, headers={**auth, "Idempotency-Key": key}, json=password_change)
            self.assertEqual(replay.json(), response.json())
            mismatch = owner.patch(url, headers={**auth, "Idempotency-Key": key},
                                   json={**password_change, "password": "different passphrase"})
            self.assertEqual(mismatch.status_code, 409)
            active_session = login(self.workspace, "technician", "replacement passphrase", now=2_000_000_000)
            self.assertIsNotNone(active_session)
            disabled = owner.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 3, "active": False})
            self.assertEqual(disabled.status_code, 200)
            self.assertEqual(disabled.json()["active"], False)
            self.assertIsNone(principal_for_session(self.workspace, active_session, now=2_000_000_001))

        self.assertIsNone(login(self.workspace, "technician", "replacement passphrase", now=2_000_000_000))
        audits = self.snapshot(
            "SELECT operation, actor_kind, actor_username, source, changes FROM audit_events "
            "WHERE entity_type='user' ORDER BY id"
        )
        self.assertEqual([row[:4] for row in audits], [
            ("user.create", "user", "owner", "web"),
            ("user.update", "user", "owner", "web"),
            ("user.update", "user", "owner", "api"),
            ("user.update", "user", "owner", "api"),
        ])
        serialized = json.dumps(audits)
        self.assertNotIn("tech passphrase", serialized)
        self.assertNotIn("replacement passphrase", serialized)
        self.assertNotIn("password_hash", serialized)

    def test_last_admin_and_concurrent_demotions_keep_one_admin(self):
        from repairlib.domain.users import UserAccess, UserError, update_user
        from repairlib.contracts import UserPatch

        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT 2,'outside','admin',password_hash FROM users WHERE id=1")
        with self.client() as client:
            _, auth = self.admin_auth(client)
            form = edit_form(client, 1, role="technician")
            refused = client.post(f"{USERS}/1/edit", data=form)
            self.assertEqual(refused.status_code, 409)
            self.assertIn("last active administrator", refused.text)
            disabled = client.post(f"{USERS}/1/edit", data=edit_form(client, 1, active=""))
            self.assertEqual(disabled.status_code, 409)
            created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json={
                "username": "second", "role": "admin", "password": "second passphrase",
            })
            self.assertEqual(created.status_code, 201, created.text)
            second_id = created.json()["id"]

        first = login(self.workspace, "owner", PASSWORD, now=2_000_000_000)
        second = login(self.workspace, "second", "second passphrase", now=2_000_000_000)

        def demote(session, target):
            try:
                return update_user(self.workspace, UserAccess("web", session), target,
                                   UserPatch(revision=1, role="technician"), key=uuid4().hex)
            except UserError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: demote(*args), [(first, second_id), (second, 1)]))
        self.assertEqual(sum(isinstance(result, int) for result in outcomes), 1)
        self.assertEqual(next(result for result in outcomes if isinstance(result, int)), 401)
        self.assertEqual(self.snapshot(
            "SELECT COUNT(*) FROM users WHERE shop_id=1 AND role='admin' AND active=1 AND password_hash <> '!'"
        ), [(1,)])
        self.assertEqual(self.snapshot(
            "SELECT COUNT(*) FROM audit_events WHERE operation='user.update' AND entity_id IN (1,?)",
            (second_id,),
        ), [(1,)])

    def test_viewer_and_second_shop_user_administration_refusals(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
            db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                       "SELECT 50,2,'hidden','admin',password_hash FROM users WHERE id=1")
        with self.client() as owner, self.client() as viewer:
            token, auth = self.admin_auth(owner)
            self.sign_in(viewer, "viewer")
            self.assertEqual(viewer.get(USERS).status_code, 403)
            self.assertEqual(viewer.get(f"{USERS}/new").status_code, 403)
            self.assertEqual(viewer.post(f"{USERS}/new", data={
                "csrf": "invalid", "key": uuid4().hex, "username": "denied",
                "role": "admin", "password": "denied passphrase",
            }).status_code, 403)
            viewer_token = issued_secret(viewer.post(ACCOUNT, data={
                "csrf": hidden(viewer.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Read only", "scope": "read", "password": PASSWORD,
            }))
            viewer_auth = {"Authorization": f"Bearer {viewer_token}"}
            self.assertEqual(viewer.get(API, headers=viewer_auth).status_code, 403)
            self.assertEqual(owner.get(API, headers=auth).status_code, 200)
            self.assertNotIn("hidden", owner.get(API, headers=auth).text)
            self.assertEqual(owner.get(f"{API}/50", headers=auth).status_code, 404)
            self.assertEqual(owner.patch(f"{API}/50", headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={"revision": 1, "role": "viewer"}).status_code, 404)
            self.assertEqual(owner.get(f"{USERS}/50/edit").status_code, 404)
        self.assertEqual(self.snapshot("SELECT id,role,active,revision FROM users WHERE id=50"),
                         [(50, "admin", 1, 1)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE entity_id=50"), [(0,)])

    def test_real_http_cli_user_create_list_edit_and_retry(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)

            def cli(*args, stdin=None, code=0):
                result = subprocess.run(
                    [*COMMAND, "users", *args, "--workspace", str(self.workspace), "--json"],
                    input=stdin, text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertNotIn("cli passphrase", result.stdout + result.stderr)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None

            key = uuid4().hex
            created = cli("create", "--username", "cli-user", "--role", "technician", "--key", key,
                          stdin="cli passphrase\n")
            replay = cli("create", "--username", "cli-user", "--role", "technician", "--key", key,
                         stdin="cli passphrase\n")
            self.assertEqual(replay, created)
            page = cli("list", "--limit", "25")
            self.assertIn(created, page["items"])
            shown = cli("show", str(created["id"]))
            self.assertEqual(shown, created)
            edited = cli("edit", str(created["id"]), "--revision", "1", "--role", "viewer",
                         "--key", uuid4().hex)
            self.assertEqual((edited["role"], edited["revision"]), ("viewer", 2))
            cli("edit", str(created["id"]), "--revision", "1", "--role", "admin",
                "--key", uuid4().hex, code=7)
            self.assertEqual(cli("show", str(created["id"])), edited)

    def test_unauthenticated_user_creation_does_no_password_work(self):
        with self.client() as client:
            with patch("repairlib.domain.passwords.PasswordHasher.hash",
                       side_effect=AssertionError("password work before authentication")), \
                 patch("repairlib.domain.users.hash_with_salt",
                       side_effect=AssertionError("password work before authentication")):
                response = client.post(API, headers={
                    "Idempotency-Key": uuid4().hex,
                    "Authorization": "Bearer rl_" + secrets.token_urlsafe(32),
                }, json={
                    "username": "unauthenticated", "role": "admin", "password": "not accepted",
                })
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM users WHERE password_hash <> '!'"), [(1,)])

    def passwd(self, username, password="new owner passphrase"):
        return subprocess.run(
            [*COMMAND, "passwd", "--workspace", str(self.workspace), "--username", username],
            input=password + "\n", text=True, capture_output=True, timeout=10, env=python_env(),
        )

    def test_shell_passwd_works_live_revokes_credentials_and_audits_cli(self):
        with self.client() as client:
            token, auth = self.admin_auth(client)
            session = client.cookies.get("repairlib_session")
            changed = self.passwd("owner")
            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertNotIn("new owner passphrase", changed.stdout + changed.stderr)
            self.assertEqual(client.get("/api/v1/identity", headers=auth).status_code, 401)
            client.cookies.set("repairlib_session", session)
            self.assertEqual(client.get("/customers").status_code, 303)
        self.assertIsNone(login(self.workspace, "owner", PASSWORD, now=2_000_000_000))
        self.assertIsNotNone(login(self.workspace, "owner", "new owner passphrase", now=2_000_000_000))
        rows = self.snapshot(
            "SELECT actor_kind,actor_username,operation,source,entity_type,entity_id,changes "
            "FROM audit_events WHERE operation='user.password'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:6], ("user", "cli", "user.password", "cli", "user", 1))
        self.assertNotIn("new owner passphrase", rows[0][6])

    def test_shell_passwd_refuses_local_actors_unknown_users_and_never_signs_them_in(self):
        people = "SELECT id, revision, password_hash FROM users WHERE password_hash <> '!' ORDER BY id"
        before = self.snapshot(people)
        for username in ("cli", "mcp", "nobody"):
            refused = self.passwd(username)
            self.assertNotEqual(refused.returncode, 0, username)
        self.assertEqual(self.snapshot(people), before)
        self.assertEqual(self.snapshot("SELECT username, password_hash FROM users WHERE password_hash = '!'"),
                         [("cli", "!")])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='user.password'"), [(0,)])
        for username in ("cli", "mcp"):
            self.assertIsNone(login(self.workspace, username, "!", now=2_000_000_000))


if __name__ == "__main__":
    unittest.main()
