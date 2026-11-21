"""S2 repair intake, assignment and attributed work-entry vertical gates."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from repairlib.domain.sessions import login
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_tokens import PASSWORD, TokenFixture
from test_users import hidden, issued_secret, ACCOUNT

REPAIRS = "/repairs"
API = "/api/v1/repairs"
CUSTOMERS = "/api/v1/customers"
USERS = "/api/v1/users"
DEVICE = {
    "device_type": "Laptop", "brand": "Synthetic", "model": "Model Γ",
    "serial": "SN-001", "display_device": "Built-in display",
    "reported_issue": "Does not power on", "notes": "Synthetic intake notes",
}


class RepairTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def prerequisites(self, client, auth):
        customer = client.post(CUSTOMERS, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"name": "Synthetic Customer"})
        self.assertEqual(customer.status_code, 201)
        technician = client.post(USERS, headers={**auth, "Idempotency-Key": uuid4().hex}, json={
            "username": "technician", "role": "technician", "password": "technician passphrase",
        })
        self.assertEqual(technician.status_code, 201)
        return customer.json(), technician.json()

    def repair_form(self, client, customer_id, technician_id, **overrides):
        page = client.get(f"{REPAIRS}/new", params={"customer_id": customer_id})
        self.assertEqual(page.status_code, 200)
        return {
            "csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
            "customer_id": hidden(page, "customer_id"), "technician_id": str(technician_id),
            **DEVICE, **overrides,
        }

    def test_browser_api_repair_entry_replay_attribution_and_restart(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            customer, technician = self.prerequisites(client, auth)
            form = self.repair_form(client, customer["id"], technician["id"])
            self.assertEqual(client.post(f"{REPAIRS}/new", data=form).status_code, 303)
            self.assertEqual(client.post(f"{REPAIRS}/new", data=form).status_code, 303)
            listing = client.get(API, headers=auth)
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(len(listing.json()["items"]), 1)
            repair = listing.json()["items"][0]
            self.assertEqual((repair["customer_id"], repair["technician_id"], repair["stage"]),
                             (customer["id"], technician["id"], "new"))
            self.assertNotIn("password", listing.text)
            detail = client.get(f"{REPAIRS}/{repair['id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Synthetic Customer", detail.text)
            # The ticket hides the assignment (owner, 2026-11-21); technician_id is checked above.
            entry_page = client.get(f"{REPAIRS}/{repair['id']}/entries/new")
            entry = {"csrf": hidden(entry_page, "csrf"), "key": hidden(entry_page, "key"),
                     "kind": "work", "body": "Opened the device and inspected the power circuit."}
            self.assertEqual(client.post(f"{REPAIRS}/{repair['id']}/entries", data=entry).status_code, 303)
            self.assertEqual(client.post(f"{REPAIRS}/{repair['id']}/entries", data=entry).status_code, 303)
            entries = client.get(f"{API}/{repair['id']}/entries", headers=auth)
            self.assertEqual(entries.status_code, 200)
            self.assertEqual(len(entries.json()["items"]), 1)
            self.assertEqual(entries.json()["items"][0]["author_username"], "owner")
            self.assertEqual(entries.json()["items"][0]["author_id"], 1)
            replay = client.post(f"{API}/{repair['id']}/entries",
                                 headers={**auth, "Idempotency-Key": entry["key"]},
                                 json={"kind": "work", "body": entry["body"]})
            self.assertEqual(replay.status_code, 201)
            self.assertEqual(replay.json(), entries.json()["items"][0])
        with self.client() as client:
            _, auth = self.admin_auth(client)
            detail = client.get(f"{API}/{repair['id']}", headers=auth).json()
            self.assertEqual(repair, {
                **detail, "customer_name": customer["name"], "technician_username": technician["username"],
            })
            self.assertEqual(client.get(f"{API}/{repair['id']}/entries", headers=auth).json(),
                             entries.json())
        self.assertEqual(self.snapshot(
            "SELECT operation,actor_username,source,entity_type,entity_id FROM audit_events "
            "WHERE operation LIKE 'repair.%' OR operation='work_entry.create' ORDER BY id"
        ), [("repair.create", "owner", "web", "repair", repair["id"]),
            ("work_entry.create", "owner", "web", "work_entry", entries.json()["items"][0]["id"])])

    def test_stale_edit_viewer_and_cross_shop_refusals(self):
        with self.client() as owner, self.client() as viewer:
            _, auth = self.admin_auth(owner)
            customer, technician = self.prerequisites(owner, auth)
            created = owner.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                 json={**DEVICE, "customer_id": customer["id"],
                                       "technician_id": technician["id"]})
            self.assertEqual(created.status_code, 201, created.text)
            repair = created.json()
            url = f"{API}/{repair['id']}"
            payload = {**DEVICE, "customer_id": customer["id"], "technician_id": None,
                       "revision": 1, "model": "Updated model"}
            key = uuid4().hex
            changed = owner.patch(url, headers={**auth, "Idempotency-Key": key}, json=payload)
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual((changed.json()["revision"], changed.json()["technician_id"]), (2, None))
            self.assertEqual(owner.patch(url, headers={**auth, "Idempotency-Key": key},
                                         json=payload).json(), changed.json())
            stale = owner.patch(url, headers={**auth, "Idempotency-Key": uuid4().hex}, json=payload)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(owner.get(url, headers=auth).json(), changed.json())

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO customers(id,shop_id,name) VALUES (50,2,'Hidden')")
                db.execute("INSERT INTO repairs(id,shop_id,customer_id,reported_issue) "
                           "VALUES (50,2,50,'Hidden issue')")
            self.assertEqual(owner.get(f"{API}/50", headers=auth).status_code, 404)
            self.assertNotIn("Hidden issue", owner.get(API, headers=auth).text)
            wrong_shop = owner.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                    json={**DEVICE, "customer_id": 50})
            self.assertEqual(wrong_shop.status_code, 422)
            self.assertEqual(owner.patch(f"{API}/50", headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json=payload).status_code, 404)

            self.sign_in(viewer, "viewer")
            self.assertEqual(viewer.get(REPAIRS).status_code, 200)
            self.assertEqual(viewer.get(f"{REPAIRS}/new").status_code, 403)
            self.assertEqual(viewer.post(f"{REPAIRS}/new", data={
                "csrf": "invalid", "key": uuid4().hex, **DEVICE,
            }).status_code, 403)
            viewer_token = issued_secret(viewer.post(ACCOUNT, data={
                "csrf": hidden(viewer.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Viewer read", "scope": "read", "password": PASSWORD,
            }))
            viewer_auth = {"Authorization": f"Bearer {viewer_token}"}
            self.assertEqual(viewer.get(API, headers=viewer_auth).status_code, 200)
            self.assertEqual(viewer.post(API, headers={**viewer_auth, "Idempotency-Key": uuid4().hex},
                                         json=DEVICE).status_code, 403)
            self.assertEqual(viewer.post(f"{API}/{repair['id']}/entries",
                                         headers={**viewer_auth, "Idempotency-Key": uuid4().hex},
                                         json={"kind": "note", "body": "Denied"}).status_code, 403)
        self.assertEqual(self.snapshot("SELECT reported_issue,revision FROM repairs WHERE id=50"),
                         [("Hidden issue", 1)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM work_entries WHERE body='Denied'"), [(0,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE shop_id=2"), [(0,)])

    def test_real_http_cli_repair_create_show_edit_entries_and_retry(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            customer, technician = self.prerequisites(client, auth)

            def cli(*args, code=0):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None

            key = uuid4().hex
            created = cli("create", "--customer", str(customer["id"]),
                          "--technician", str(technician["id"]), "--key", key,
                          "--device-type", "Laptop", "--reported-issue", "CLI intake")
            replay = cli("create", "--customer", str(customer["id"]),
                         "--technician", str(technician["id"]), "--key", key,
                         "--device-type", "Laptop", "--reported-issue", "CLI intake")
            self.assertEqual(replay, created)
            self.assertEqual(cli("show", str(created["id"])), created)
            self.assertEqual(cli("list")["items"], [{
                **created, "customer_name": customer["name"], "technician_username": technician["username"],
            }])
            edited = cli("edit", str(created["id"]), "--revision", "1", "--key", uuid4().hex,
                         "--model", "CLI model", "--clear-technician")
            self.assertEqual((edited["model"], edited["technician_id"], edited["revision"]),
                             ("CLI model", None, 2))
            entry_key = uuid4().hex
            entry = cli("add-entry", str(created["id"]), "--kind", "note",
                        "--body", "CLI work entry", "--key", entry_key)
            self.assertEqual(cli("add-entry", str(created["id"]), "--kind", "note",
                                 "--body", "CLI work entry", "--key", entry_key), entry)
            self.assertEqual(cli("entries", str(created["id"]))["items"], [entry])
            cli("edit", str(created["id"]), "--revision", "1", "--key", uuid4().hex,
                "--model", "Stale", code=7)
            self.assertEqual(cli("show", str(created["id"])), edited)
