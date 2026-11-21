"""Customer lifecycle parity through the authenticated adapters."""

from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx

from repairlib.adapters.web.security import SESSION_COOKIE, csrf_token
from repairlib.contracts import CustomerInput
from repairlib.domain.customers import CustomerAccess, change_customer_archive, create_customer, read_customer
from repairlib.lifecycle.workspace import load_form_key
from repairlib.storage import initialization

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_customers import API
from test_tokens import ACCOUNT, TokenFixture, hidden, issued_secret


class CustomerLifecycleTests(TokenFixture, TestCase):
    def archive_post_data(self, client, customer):
        # The UI no longer emits an archive form for an active customer.
        token = csrf_token(bytearray(load_form_key(self.workspace)),
                           f"customer.archive:{customer['id']}", client.cookies[SESSION_COOKIE])
        return {"csrf": token, "revision": str(customer["revision"]), "enabled": "1", "key": uuid4().hex}

    def auth(self, client, **options):
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, **options)))
        return {"Authorization": f"Bearer {token}"}

    def create(self, client, auth, name="Synthetic target"):
        response = client.post(API, json={"name": name}, headers={**auth, "Idempotency-Key": uuid4().hex})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_api_archive_restore_revision_replay_and_filters(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            reader = self.auth(client, scope="read")
            older = self.create(client, auth, "Synthetic older")
            customer = self.create(client, auth)
            path = f"{API}/{customer['id']}"
            body = {"revision": 1, "enabled": True}
            headers = {**auth, "Idempotency-Key": "archive-test"}
            change = client.post(path + "/archive", json=body, headers=headers)
            self.assertEqual(change.status_code, 200)
            archived = change.json()
            self.assertEqual(archived["revision"], 2)
            self.assertTrue(archived["archived_at"].endswith("Z"))
            self.assertEqual(client.post(path + "/archive", json=body, headers=headers).json(), archived)
            self.assertEqual(client.post(path + "/archive", json={**body, "enabled": False},
                                         headers=headers).json()["error"]["code"], "conflict")
            self.assertEqual(client.post(path + "/archive", json=body, headers={
                **reader, "Idempotency-Key": "archive-test",
            }).status_code, 403)
            fresh = {**auth, "Idempotency-Key": "restore-test"}
            self.assertEqual(client.post(path + "/archive", json=body, headers=fresh)
                             .json()["error"]["code"], "stale_revision")
            self.assertEqual(client.post(path + "/archive", json={**body, "revision": 2},
                                         headers=fresh).status_code, 409)
            for invalid in ({**body, "actor": "forged"}, {**body, "revision": True}, {"revision": 2}):
                self.assertEqual(client.post(path + "/archive", json=invalid, headers=fresh).status_code, 422)
            self.assertEqual(client.get(API, headers=reader).json()["items"], [{**older, "open_repair_count": 0}])
            params = {"include_archived": True, "q": "synthetic", "limit": 1}
            page = client.get(API, params=params, headers=reader).json()
            self.assertEqual(page["items"], [{**archived, "open_repair_count": 0}])
            self.assertEqual(client.get(API, params={**params, "before": page["next_before"]},
                                        headers=reader).json()["items"], [{**older, "open_repair_count": 0}])
        with self.client() as client:
            self.assertEqual(client.get(path, headers=reader).json(), archived)
            restore = client.post(path + "/archive", json={"revision": 2, "enabled": False}, headers=fresh)
            self.assertEqual(restore.status_code, 200)
            active = restore.json()
            self.assertEqual(active["revision"], 3)
            self.assertIsNone(active["archived_at"])
            self.assertEqual(client.post(path + "/archive", json=body, headers=headers).json(), archived)
            self.assertEqual(client.get(API, headers=reader).json()["items"],
                             [{**active, "open_repair_count": 0}, {**older, "open_repair_count": 0}])
        self.assertEqual(self.snapshot("SELECT operation, actor_username, source, token_label FROM audit_events "
                                       "WHERE operation IN ('customer.archive', 'customer.restore') ORDER BY id"),
                         [("customer.archive", "owner", "api", "Workshop CLI"),
                          ("customer.restore", "owner", "api", "Workshop CLI")])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customer_requests"), [(4,)])

    def test_populated_schema_thirteen_keeps_previous_results_after_upgrade(self):
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            db_path = workspace / "state" / self.db.name
            db_path.parent.mkdir()
            password_hash, = self.snapshot("SELECT password_hash FROM users WHERE id=1")[0]
            initialization.initialize_database(db_path, "Prior shop", "UTC", "owner",
                                               password_hash, target=13)
            details = CustomerInput("Synthetic prior")
            payload = asdict(details)
            fingerprint = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            token = "rl_" + secrets.token_urlsafe(32)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("INSERT INTO customers(id,shop_id,name) VALUES (1,1,?)", (details.name,))
                created_at, = db.execute("SELECT created_at FROM customers").fetchone()
                result = {"id": 1, "revision": 1, "created_at": created_at, **payload}
                db.execute("INSERT INTO customer_requests VALUES (1,1,'customer.create','old',?,1,?)",
                           (fingerprint, json.dumps(result)))
                db.execute("INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key) "
                           "VALUES (1,1,1,?,'Prior profile','write','prior')", (sha256(token.encode()).hexdigest(),))

            apply = initialization.apply_migration

            def interrupted(db, version):
                apply(db, version)
                raise sqlite3.OperationalError("Synthetic interrupted upgrade")

            with patch.object(initialization, "apply_migration", interrupted):
                with self.assertRaises(initialization.UpgradeError):
                    initialization.upgrade_database(db_path)
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (13,))
                self.assertNotIn("archived_at", [row[1] for row in db.execute("PRAGMA table_info(customers)")])
            initialization.upgrade_database(db_path)
            access = CustomerAccess("api", token)
            record, _ = read_customer(workspace, access, 1)
            self.assertEqual(asdict(record), {**result, "archived_at": None})
            self.assertEqual(create_customer(workspace, access, details, key="old"), record)

    def test_history_is_attributed_paginated_and_shop_scoped(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Other shop')")
            db.execute("INSERT INTO customers(id,shop_id,name) VALUES (20,2,'Other customer')")
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT 1,'viewer','viewer',password_hash FROM users WHERE id=1")
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT 1,'tech','technician',password_hash FROM users WHERE id=1")
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            path = f"{API}/{customer['id']}"
            self.assertEqual(client.patch(path, json={"revision": 1, "name": "Updated <name>"},
                                         headers={**auth, "Idempotency-Key": "edit"}).status_code, 200)
        with self.client() as client:
            self.sign_in(client, "tech")
            tech = self.auth(client, name="Tech CLI")
            body = {"revision": 2, "enabled": True}
            self.assertEqual(client.post(path + "/archive", json=body,
                                         headers={**tech, "Idempotency-Key": "archive"}).status_code, 200)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE users SET username='former-tech', active=0 WHERE username='tech'")
        with self.client() as client:
            self.assertEqual(client.get(path + "/history").status_code, 401)
            self.sign_in(client, "viewer")
            viewer = self.auth(client, scope="read")
            web_detail = client.get(f"/customers/{customer['id']}")
            self.assertEqual(web_detail.status_code, 200)
            self.assertIn("Updated &lt;name&gt;", web_detail.text)
            self.assertNotIn("Restore customer", web_detail.text)
            self.assertEqual(client.get("/customers/20").status_code, 404)
            self.assertEqual(client.post(path + "/archive", json={"revision": 3, "enabled": False},
                                         headers={**viewer, "Idempotency-Key": "restore"}).status_code, 403)
            self.assertEqual(client.get(API + "/20/history", headers=auth).status_code, 404)
            self.assertEqual(client.post(API + "/20/archive", json=body,
                                         headers={**auth, "Idempotency-Key": "archive"}).status_code, 404)
            self.assertEqual(client.get(path + "/history", headers=viewer, params={"limit": 101}).status_code, 422)
            response = client.get(path + "/history", headers=viewer, params={"limit": 1})
            self.assertEqual(response.status_code, 200)
            page = response.json()
            archive, = page["items"]
            self.assertEqual(archive["operation"], "customer.archive")
            self.assertEqual(archive["actor_username"], "tech")
            self.assertEqual(archive["actor_user_id"], 3)
            self.assertEqual(archive["source"], "api")
            self.assertEqual(archive["token_label"], "Tech CLI")
            self.assertTrue(archive["occurred_at"].endswith("Z"))
            self.assertEqual(archive["changes"], {"archived_at": {
                "before": None, "after": client.get(path, headers=viewer).json()["archived_at"],
            }})
            rest = client.get(path + "/history", headers=viewer,
                              params={"before": page["next_before"]}).json()
            self.assertEqual([event["operation"] for event in rest["items"]],
                             ["customer.update", "customer.create"])
            self.assertEqual(rest["items"][0]["changes"],
                             {"name": {"before": "Synthetic target", "after": "Updated <name>"}})
            self.assertEqual(rest["items"][1]["actor_username"], "owner")
            self.assertIsNone(rest["next_before"])
        self.assertEqual(self.snapshot("SELECT id, revision, archived_at FROM customers WHERE shop_id=2"),
                         [(20, 1, None)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customer_requests"), [(3,)])

    def test_archive_button_hidden_and_restore_form_keeps_readable_history(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            path = f"/customers/{customer['id']}"
            self.assertIn(f'href="{path}"', client.get("/customers").text)
            detail = client.get(path)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Customer history", detail.text)
            self.assertTrue("Ιστορικό πελάτη" in client.get(path, headers={"Accept-Language": "el"}).text)
            self.assertIn("owner", detail.text)
            self.assertNotIn(f'action="{path}/archive"', detail.text)
            form = self.archive_post_data(client, customer)
            self.assertEqual(client.post(path + "/archive", data={**form, "csrf": "wrong"}).status_code, 403)
            self.assertEqual(client.post(path + "/archive", data={**form, "enabled": "x"}).status_code, 422)
            self.assertEqual(client.post(path + "/archive", data={**form, "revision": "2"}).status_code, 409)
            self.assertEqual(client.post(path + "/archive", data=form).headers["location"], path)
            self.assertEqual(client.post(path + "/archive", data=form).status_code, 303)
            self.assertNotIn(customer["name"], client.get("/customers").text)
            included = client.get("/customers", params={"include_archived": True, "q": "target"})
            self.assertIn(customer["name"], included.text)
            self.assertIn("Archived", included.text)
            detail = client.get(path)
            self.assertIn("Restore customer", detail.text)
            self.assertIn("Archived customer", detail.text)
            self.assertIn("Web", detail.text)
            form = {name: hidden(detail, name) for name in ("csrf", "key", "revision", "enabled")}
            self.assertEqual(form["enabled"], "0")
            self.assertEqual(client.post(path + "/archive", data=form).status_code, 303)
            self.assertIn("Restored customer", client.get(path).text)
            self.assertIn(customer["name"], client.get("/customers").text)
        self.assertEqual(self.snapshot("SELECT source FROM audit_events WHERE operation IN "
                                       "('customer.archive', 'customer.restore') ORDER BY id"),
                         [("web",), ("web",)])

    def test_real_http_cli_archive_restore_and_history_survive_restart(self):

        def cli(*args, stdin=None, status=0):
            result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                    text=True, capture_output=True, timeout=10, env=python_env())
            self.assertTrue(token not in result.stdout + result.stderr)
            self.assertEqual(result.returncode, status, result.stderr)
            if "--json" in args and status == 0:
                return json.loads(result.stdout)
            return result.stdout

        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            auth = self.auth(client)
            token = auth["Authorization"].removeprefix("Bearer ")
            customer = self.create(client, auth)
            customer_id = str(customer["id"])
            archive = ("customers", "archive", customer_id, "--revision", "1", "--key", "cli-archive", "--json")
            archived = cli(*archive)
            self.assertTrue(archived["archived_at"])
            self.assertEqual(cli("customers", "list", "--json")["items"], [])
            self.assertEqual(cli("customers", "list", "--include-archived", "--json")["items"],
                             [{**archived, "open_repair_count": 0}])
        with workspace_client(self.workspace) as (client, _):
            self.assertEqual(cli("customers", "show", customer_id, "--json"), archived)
            self.assertIn("Archived", cli("customers", "show", customer_id))
            self.assertEqual(cli(*archive), archived)
            page = cli("customers", "history", customer_id, "--limit", "1", "--json")
            self.assertEqual((page["items"][0]["actor_username"], page["items"][0]["source"],
                              page["items"][0]["token_label"]), ("cli", "cli", None))
            older = cli("customers", "history", customer_id, "--before", str(page["next_before"]), "--json")
            self.assertEqual(older["items"][0]["operation"], "customer.create")
            restored = cli("customers", "restore", customer_id, "--revision", "2", "--key", "cli-restore", "--json")
            self.assertIsNone(restored["archived_at"])
            self.assertEqual(restored["revision"], 3)
            self.assertEqual(cli("customers", "list", "--json")["items"],
                             [{**restored, "open_repair_count": 0}])
            self.assertIn("customer.restore", cli("customers", "history", customer_id))

    def archive(self, client, auth, customer_id):
        response = client.post(f"{API}/{customer_id}/archive", json={"revision": 1, "enabled": True},
                               headers={**auth, "Idempotency-Key": "archive"})
        self.assertEqual(response.status_code, 200)

    def test_api_create_repair_refuses_archived_customer_without_side_effects(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            self.archive(client, auth, customer["id"])
            audits = self.snapshot("SELECT * FROM audit_events ORDER BY id")
            response = client.post("/api/v1/repairs", headers={**auth, "Idempotency-Key": "repair"}, json={
                "customer_id": customer["id"], "reported_issue": "Synthetic repair",
            })
            self.assertEqual(response.status_code, 422)
            error = response.json()["error"]
            self.assertEqual(error["code"], "validation")
            self.assertIn("customer_id", error["fields"])
        self.assertEqual(self.snapshot("SELECT * FROM repairs"), [])
        self.assertEqual(self.snapshot("SELECT * FROM repair_requests"), [])
        self.assertEqual(self.snapshot("SELECT * FROM audit_events ORDER BY id"), audits)

    def test_api_update_repair_refuses_archived_customer_without_side_effects(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            self.archive(client, auth, customer["id"])
            created = client.post("/api/v1/repairs", headers={**auth, "Idempotency-Key": "repair"},
                                  json={"reported_issue": "Synthetic repair"})
            self.assertEqual(created.status_code, 201)
            repair = created.json()
            path = f"/api/v1/repairs/{repair['id']}"
            audits = self.snapshot("SELECT * FROM audit_events ORDER BY id")
            requests = self.snapshot("SELECT * FROM repair_requests")
            response = client.patch(path, headers={**auth, "Idempotency-Key": "assign"}, json={
                "revision": repair["revision"], "customer_id": customer["id"],
                "reported_issue": "Updated synthetic repair",
            })
            self.assertEqual(response.status_code, 422)
            error = response.json()["error"]
            self.assertEqual(error["code"], "validation")
            self.assertIn("customer_id", error["fields"])
        with self.client() as client:
            self.assertEqual(client.get(path, headers=auth).json(), repair)
        self.assertEqual(self.snapshot("SELECT * FROM repair_requests"), requests)
        self.assertEqual(self.snapshot("SELECT * FROM audit_events ORDER BY id"), audits)

    def test_archive_hides_customer_from_intake_picker_and_preserves_existing_link(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            customer_id = customer["id"]
            headers = {**auth, "Idempotency-Key": "repair"}
            payload = {"customer_id": customer_id, "reported_issue": "Synthetic repair"}
            repaired = client.post("/api/v1/repairs", headers=headers, json=payload)
            self.assertEqual(repaired.status_code, 201)
            repair_id = repaired.json()["id"]
            self.archive(client, auth, customer_id)
            replay = client.post("/api/v1/repairs", headers=headers, json=payload)
            self.assertEqual(replay.status_code, 201)
            self.assertEqual(replay.json(), repaired.json())
            self.assertNotIn(customer["name"], client.get("/repairs/new").text)
            repair_path = f"/repairs/{repair_id}/edit"
            edit = client.get(repair_path)
            self.assertIn(f'<option value="{customer_id}" selected', edit.text)
            body = {name: hidden(edit, name) for name in ("csrf", "key", "revision")}
            response = client.post(repair_path, data={**body, "customer_id": customer_id,
                                                     "reported_issue": "Updated synthetic repair"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(client.get(f"/api/v1/repairs/{repair_id}",
                                        headers=auth).json()["customer_id"], customer_id)

    def test_failed_archive_audit_rolls_back_and_browser_retries_lost_response(self):
        with self.client() as client:
            self.sign_in(client)
            auth = self.auth(client)
            customer = self.create(client, auth)
            path = f"/customers/{customer['id']}"
            form = self.archive_post_data(client, customer)

            with patch("repairlib.storage.customers.audit", side_effect=sqlite3.IntegrityError("Synthetic failure")):
                self.assertEqual(client.post(path + "/archive", data=form).status_code, 500)
            self.assertEqual(self.snapshot("SELECT revision, archived_at FROM customers"), [(1, None)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customer_requests"), [(1,)])

            def commit_then_disconnect(*args, **kwargs):
                change_customer_archive(*args, **kwargs)
                raise httpx.ReadError("Synthetic response loss")

            with patch("repairlib.adapters.web.customers.customers.change_customer_archive", commit_then_disconnect):
                self.assertEqual(client.post(path + "/archive", data=form).status_code, 500)

        with self.client() as client:
            self.sign_in(client)
            self.assertEqual(client.post(path + "/archive", data=form).status_code, 403)
            retry = client.post(f"{API}/{customer['id']}/archive", json={"revision": 1, "enabled": True},
                                headers={**auth, "Idempotency-Key": form["key"]})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json()["revision"], 2)
        self.assertEqual(self.snapshot("SELECT source FROM audit_events WHERE operation='customer.archive'"),
                         [("web",)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customer_requests"), [(2,)])
