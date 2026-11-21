"""Contact normalization, warnings and migration through synthetic workspaces."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from repairlib.contracts import CustomerInput
from repairlib.domain import customers, repairs, users
from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_customers import API as CUSTOMERS
from test_repairs import API as REPAIRS, DEVICE
from test_tokens import ACCOUNT, TokenFixture, hidden, issued_secret


class CustomerContactTests(TokenFixture, TestCase):
    def authenticate(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def post(self, client, route, auth, payload, *, key=None):
        result = client.post(route, headers={**auth, "Idempotency-Key": key or uuid4().hex}, json=payload)
        self.assertEqual(result.status_code, 201)
        return result.json()

    def web_create(self, client, fields, *, intake=False):
        page = client.get("/customers/new", params={"next": "new-repair"} if intake else {})
        form = {**fields, "key": hidden(page, "key"), "csrf": hidden(page, "csrf")}
        if intake:
            form["next"] = "new-repair"
        response = client.post("/customers/new", data=form)
        self.assertEqual(response.status_code, 303)
        return response.headers["location"]

    def test_digit_search_and_update_keep_raw_phone_and_shop_scope(self):
        with self.client() as client:
            auth = self.authenticate(client)
            phone = "+357 (99) 123-456"
            contact = self.post(client, CUSTOMERS, auth, {"name": "Synthetic contact", "phone": phone})
            self.assertEqual(contact["phone"], phone)
            repair = self.post(client, REPAIRS, auth, {**DEVICE, "customer_id": contact["id"]})
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside')")
                db.execute("INSERT INTO customers(id, shop_id, name, phone) VALUES (99, 2, 'Hidden', ?)",
                           (phone,))
                db.execute("INSERT INTO repairs(id, shop_id, customer_id, reported_issue) "
                           "VALUES (99, 2, 99, 'Hidden repair')")
            for query in ("99 123456", "99123456", "+357-99-123456"):
                for route, expected in ((CUSTOMERS, contact), (REPAIRS, repair)):
                    results = client.get(route, headers=auth, params={"q": query}).json()
                    self.assertEqual([row["id"] for row in results["items"]], [expected["id"]])
            self.assertIn("Synthetic contact", client.get("/customers", params={"q": "99 123456"}).text)
            self.assertIn("Synthetic contact", client.get("/repairs", params={"q": "99 123456"}).text)
            self.assertEqual(client.get(f"{CUSTOMERS}/99", headers=auth).status_code, 404)
            updated = client.patch(f"{CUSTOMERS}/{contact['id']}", headers={**auth, "Idempotency-Key": "edit"},
                                   json={"revision": 1, "phone": "99 / 456 789"})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(self.snapshot("SELECT phone, phone_digits FROM customers WHERE shop_id=1"),
                             [("99 / 456 789", "99456789")])
            for route, expected in ((CUSTOMERS, contact), (REPAIRS, repair)):
                self.assertEqual(client.get(route, headers=auth, params={"q": "99123456"}).json()["items"], [])
                found = client.get(route, headers=auth, params={"q": "99 456789"}).json()
                self.assertEqual([row["id"] for row in found["items"]], [expected["id"]])

    def test_shape_warnings_on_create_edit_and_existing_records(self):
        with self.client() as client:
            auth = self.authenticate(client)
            fields = {"name": "Synthetic contact", "phone": "Ask at desk", "email": "no address"}
            created = self.post(client, CUSTOMERS, auth, fields)
            self.assertEqual(set(created["contact_warnings"]), {"phone", "email"})
            url = f"/customers/{created['id']}"
            readback = client.get(f"/api/v1{url}", headers=auth).json()
            self.assertEqual(readback, created)
            for path in (url, f"{url}/edit"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                for warning in created["contact_warnings"].values():
                    self.assertIn(warning, page.text)
            repaired = client.patch(f"/api/v1{url}", headers={**auth, "Idempotency-Key": "fix-email"},
                                    json={"revision": 1, "email": "name@example.test"})
            self.assertEqual(repaired.status_code, 200)
            self.assertEqual(set(repaired.json()["contact_warnings"]), {"phone"})
            self.assertEqual(repaired.json()["phone"], fields["phone"])
            page = client.get(self.web_create(client, {**fields, "email": "local@host"}))
            self.assertIn(created["contact_warnings"]["email"], page.text)
            self.assertIn("local@host", page.text)

    def test_duplicate_notice_is_nonblocking_shop_scoped_and_replay_stable(self):
        with self.client() as client:
            auth = self.authenticate(client)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside')")
                db.execute("INSERT INTO customers(id, shop_id, name, phone, email) "
                           "VALUES (99, 2, 'Hidden', '99123456', 'one@example.test')")
            first = self.post(client, CUSTOMERS, auth, {"name": "Synthetic original", "phone": "99 123 456",
                                                        "email": "One@Example.test"})
            self.assertIsNone(first.get("possible_duplicate_id"))
            fields = {"name": "Synthetic second", "phone": "(99) 123-456"}
            duplicate = self.post(client, CUSTOMERS, auth, fields, key="duplicate")
            self.assertNotEqual(duplicate["id"], first["id"])
            self.assertEqual(duplicate["possible_duplicate_id"], first["id"])
            self.assertEqual(self.post(client, CUSTOMERS, auth, fields, key="duplicate"), duplicate)
            wrong_record = client.get(f"/customers/{first['id']}", params={"customer_receipt": "duplicate"})
            self.assertNotIn("Possible duplicate customer", wrong_record.text)
            for intake in (False, True):
                page = client.get(self.web_create(client, {"name": "Synthetic by email",
                                                           "email": "one@example.test"}, intake=intake))
                self.assertEqual(page.status_code, 200)
                self.assertIn("Possible duplicate customer", page.text)
                self.assertIn(f'href="/customers/{first["id"]}"', page.text)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customers WHERE shop_id=1"), [(4,)])

    def test_local_cli_reports_notices_on_success(self):
        def cli(*args):
            result = subprocess.run([*COMMAND, "customers", *args, "--workspace", str(self.workspace)],
                                    text=True, capture_output=True, timeout=10, env=python_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        original = cli("create", "--name", "Synthetic original", "--phone", "+357 99123456",
                       "--key", "original", "--json")
        original_id = json.loads(original.stdout)["id"]
        duplicate = cli("create", "--name", "Synthetic duplicate", "--phone", "+357 (99) 123-456",
                        "--email", "Ask at desk", "--key", "duplicate")
        self.assertIn(f"Possible duplicate customer {original_id}.", duplicate.stderr)
        self.assertIn("Check the email address.", duplicate.stderr)
        results = json.loads(cli("list", "--query", "99 123456", "--json").stdout)
        self.assertEqual(len(results["items"]), 2)

    def test_populated_schema_27_upgrade_is_atomic_and_preserves_contact_and_repair(self):
        workspace = self.workspace.parent / "prior"
        path = workspace / "state" / self.db.name
        path.parent.mkdir(parents=True)
        password_hash, = self.snapshot("SELECT password_hash FROM users WHERE id=1")[0]
        initialization.initialize_database(path, "Synthetic prior", "UTC", "owner", password_hash, target=27)
        phone = "+357 (99) 12-34.56"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO customers(id, shop_id, name, phone, email, revision) "
                       "VALUES (1, 1, 'Synthetic legacy', ?, 'legacy address', 4)", (phone,))
            db.execute("INSERT INTO repairs(id, shop_id, customer_id, reported_issue) "
                       "VALUES (1, 1, 1, 'Synthetic repair')")
        apply = initialization.apply_migration

        def interrupt(db, version):
            apply(db, version)
            raise sqlite3.OperationalError("Synthetic interruption")

        with patch.object(initialization, "apply_migration", interrupt):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (27,))
            self.assertNotIn("phone_digits", [column[1] for column in db.execute("PRAGMA table_info(customers)")])

        for _ in range(2):
            initialization.upgrade_database(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (initialization.SCHEMA_VERSION,))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT phone, email, revision, phone_digits FROM customers").fetchall(),
                             [(phone, "legacy address", 4, "35799123456")])
            self.assertTrue(any([column[2] for column in db.execute(
                "SELECT * FROM pragma_index_info(?)", (index[1],)
            )] == ["shop_id", "phone_digits"] for index in db.execute("PRAGMA index_list(customers)")))
        users.ensure_local_actor(workspace, "cli")
        access = customers.CustomerAccess("cli")
        page, _ = customers.list_customers(workspace, access, query="99 123456")
        self.assertEqual([row.id for row in page.items], [1])
        page, _ = repairs.list_repairs(workspace, access, query="99 123456")
        self.assertEqual([row.id for row in page.items], [1])
        legacy, _ = customers.read_customer(workspace, access, 1)
        self.assertEqual(set(legacy.contact_warnings), {"email"})
        created = customers.create_customer(workspace, access, CustomerInput("Fresh"), key="after-upgrade")
        self.assertEqual(created.id, 2)
