"""S4 catalog adapters: JSON API, browser forms and authenticated HTTP CLI parity."""

from contextlib import closing
import json
import secrets
import sqlite3
import subprocess
from unittest import TestCase
from uuid import uuid4

from repairlib.domain.tokens import api_token_hash
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
import test_tokens
from test_tokens import ACCOUNT, hidden, issued_secret

API = "/api/v1/inventory"
ITEM = {"sku": "RC-1", "description": "Synthetic <capacitor>", "category": "Components",
        "unit": "pcs", "low_stock_milli": 1250, "purchase_cost_cents": 17, "selling_price_cents": 42}


class InventoryAdapterTests(test_tokens.TokenFixture, TestCase):
    def add_user(self, username, role, shop_id=1):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT OR IGNORE INTO shops(id,name) VALUES (?, 'Other shop')", (shop_id,))
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT ?,?,?,password_hash FROM users WHERE id=1", (shop_id, username, role))

    def token(self, client, username="owner", scope="write"):
        self.sign_in(client, username)
        return {"Authorization": "Bearer " + issued_secret(
            client.post(ACCOUNT, data=self.issue_form(client, scope=scope)))}

    def direct_token(self, username):
        secret = "rl_" + secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO api_tokens(shop_id,user_id,user_revision,token_hash,name,scope,create_key) "
                       "SELECT shop_id,id,revision,?,'Other shop','write','other' FROM users WHERE username=?",
                       (api_token_hash(secret), username))
        return {"Authorization": f"Bearer {secret}"}

    def test_api_web_parity_redaction_refusals_and_restart(self):
        self.add_user("viewer", "viewer")
        self.add_user("other", "admin", shop_id=2)
        with self.client() as client:
            self.assertEqual(client.get(API).status_code, 401)
            auth = self.token(client)
            self.assertIn('href="/inventory"', client.get("/").text)
            self.assertIn('href="/inventory"', client.get(ACCOUNT.rsplit("/", 1)[0]).text)
            key = uuid4().hex
            created = client.post(API, headers={**auth, "Idempotency-Key": key}, json=ITEM)
            self.assertEqual(created.status_code, 201, created.text)
            item = created.json()
            self.assertEqual((item["revision"], item["purchase_cost_cents"]), (1, 17))
            self.assertEqual(client.post(API, headers={**auth, "Idempotency-Key": key}, json=ITEM).json(), item)
            self.assertEqual(client.post(API, headers={**auth, "Idempotency-Key": key},
                                         json={**ITEM, "sku": "Other"}).status_code, 409)
            for body, code in (({**ITEM, "shop_id": 2}, "validation"), (ITEM, "duplicate_sku"),
                               ({**ITEM, "sku": "F", "selling_price_cents": 1.5}, "validation")):
                reply = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=body)
                self.assertEqual(reply.json()["error"]["code"], code)

            form = client.get("/inventory/new")
            self.assertEqual(form.status_code, 200)
            fields = {"csrf": hidden(form, "csrf"), "key": hidden(form, "key"), "sku": "WEB-1",
                      "description": "Web fuse", "unit": "pcs", "low_stock": "2.5",
                      "purchase_cost": "1.05", "selling_price": "3"}
            invalid = client.post("/inventory/new", data={**fields, "selling_price": "1.234"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('value="Web fuse"', invalid.text)
            self.assertEqual(client.post("/inventory/new", data={**fields, "csrf": "bad"}).status_code, 403)
            self.assertEqual(client.post("/inventory/new", data=fields).status_code, 303)
            self.assertEqual(client.post("/inventory/new", data=fields).status_code, 303)
            web_item = client.get(API, headers=auth).json()["items"][0]
            self.assertEqual((web_item["sku"], web_item["low_stock_milli"], web_item["purchase_cost_cents"],
                              web_item["selling_price_cents"]), ("WEB-1", 2500, 105, 300))
            self.assertIn("Synthetic &lt;capacitor&gt;", client.get("/inventory").text)

            path = f"{API}/{item['id']}"
            edit = {**ITEM, "selling_price_cents": 50, "revision": 1}
            self.assertEqual(client.patch(path, headers={**auth, "Idempotency-Key": uuid4().hex},
                                          json={**edit, "revision": 9}).json()["error"]["code"], "stale_revision")
            edited = client.patch(path, headers={**auth, "Idempotency-Key": uuid4().hex}, json=edit)
            self.assertEqual((edited.status_code, edited.json()["revision"]), (200, 2))

            page = client.get(f"/inventory/{web_item['id']}/edit")
            web_edit = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                        "revision": hidden(page, "revision"), "sku": "WEB-1", "description": "Web fuse 2A",
                        "unit": "pcs", "low_stock": "2.5", "purchase_cost": "", "selling_price": "3"}
            stale = client.post(f"/inventory/{web_item['id']}/edit", data={**web_edit, "revision": "7"})
            self.assertEqual(stale.status_code, 409)
            self.assertIn("Open latest version", stale.text)
            self.assertEqual(client.post(f"/inventory/{web_item['id']}/edit", data=web_edit).status_code, 303)
            detail = client.get(f"/inventory/{web_item['id']}")
            archive = {"csrf": hidden(detail, "csrf"), "key": hidden(detail, "key"),
                       "revision": hidden(detail, "revision")}
            self.assertEqual(client.post(f"/inventory/{web_item['id']}/archive", data=archive).status_code, 303)
            self.assertEqual(client.post(f"/inventory/{web_item['id']}/archive", data=archive).status_code, 303)
            web_path = f"{API}/{web_item['id']}"
            archived = client.get(web_path, headers=auth).json()
            self.assertEqual((archived["revision"], archived["purchase_cost_cents"],
                              archived["description"]), (3, None, "Web fuse 2A"))
            self.assertIsNotNone(archived["archived_at"])
            self.assertEqual(client.post(f"{web_path}/archive", headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={"revision": 3}).json()["error"]["code"], "archived")
            self.assertEqual([i["sku"] for i in client.get(API, headers=auth).json()["items"]], ["RC-1"])
            self.assertEqual([i["sku"] for i in client.get(API, headers=auth, params={
                "include_archived": "true"}).json()["items"]], ["WEB-1", "RC-1"])
            listing = client.get(API, headers=auth, params={"limit": 1, "include_archived": "true"}).json()
            self.assertEqual(listing["next_before"], web_item["id"])
            self.assertEqual(client.get(API, headers=auth, params={"limit": "0"}).status_code, 422)

        with closing(sqlite3.connect(self.db)) as db, db:
            outside = db.execute("INSERT INTO inventory_items(shop_id,sku,description,unit,purchase_cost_cents) "
                                 "VALUES (2,'RC-1','Outside part','pcs',999)").lastrowid
        with self.client() as client:
            self.assertEqual(client.get(path, headers=auth).json()["revision"], 2)
            self.assertEqual(client.get(f"{API}/{outside}", headers=auth).status_code, 404)
            self.assertEqual(client.patch(f"{API}/{outside}", headers={**auth, "Idempotency-Key": uuid4().hex},
                                          json={**edit, "revision": 1}).status_code, 404)
            self.assertEqual(self.snapshot("SELECT revision, description FROM inventory_items WHERE shop_id=2"),
                             [(1, "Outside part")])
            other = self.direct_token("other")
            self.assertEqual(client.get(path, headers=other).status_code, 401)
            self.assertEqual(client.patch(path, headers={**other, "Idempotency-Key": uuid4().hex},
                                          json={**edit, "revision": 2}).status_code, 401)
        with self.client() as client:
            viewer = self.token(client, "viewer", scope="read")
            api_item = client.get(path, headers=viewer).json()
            self.assertIsNone(api_item["purchase_cost_cents"])
            self.assertEqual(api_item["selling_price_cents"], 50)
            self.assertTrue(all(i["purchase_cost_cents"] is None
                                for i in client.get(API, headers=viewer).json()["items"]))
            self.assertEqual(client.post(API, headers={**viewer, "Idempotency-Key": uuid4().hex},
                                         json={**ITEM, "sku": "V"}).status_code, 403)
            self.assertEqual(client.get("/inventory/new").status_code, 403)
            self.assertEqual(client.get(f"/inventory/{item['id']}/edit").status_code, 403)
            for page in (client.get("/inventory"), client.get(f"/inventory/{item['id']}")):
                self.assertEqual(page.status_code, 200)
                self.assertIn("0.50", page.text)
                self.assertNotIn("0.17", page.text)
                self.assertNotIn("Purchase cost", page.text)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_items WHERE shop_id=1"), [(2,)])
        self.assertEqual(self.snapshot(
            "SELECT operation, source FROM audit_events WHERE entity_type='inventory_item' ORDER BY id"), [
            ("inventory.create", "api"), ("inventory.create", "web"), ("inventory.edit", "api"),
            ("inventory.edit", "web"), ("inventory.archive", "web")])

    def test_real_cli_create_replay_edit_archive_and_list(self):
        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))

            def cli(*args, stdin=None, code=0):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if "--json" in args and code == 0 else result

            create = ("inventory", "create", "--sku", "CLI-1", "--description", "CLI resistor",
                      "--purchase-cost", "0.05", "--selling-price", "0.20", "--low-stock", "10",
                      "--key", uuid4().hex, "--json")
            created = cli(*create)
            self.assertEqual(cli(*create), created)
            self.assertEqual((created["purchase_cost_cents"], created["selling_price_cents"],
                              created["low_stock_milli"], created["unit"]), (5, 20, 10000, "pcs"))
            item = str(created["id"])
            self.assertEqual(cli("inventory", "show", item, "--json"), created)
            cli("inventory", "edit", item, "--revision", "9", "--bin", "B2", "--key", uuid4().hex, code=7)
            edited = cli("inventory", "edit", item, "--revision", "1", "--bin", "B2",
                         "--key", uuid4().hex, "--json")
            self.assertEqual((edited["revision"], edited["bin"], edited["purchase_cost_cents"]), (2, "B2", 5))
            archived = cli("inventory", "archive", item, "--revision", "2", "--key", uuid4().hex, "--json")
            self.assertIsNotNone(archived["archived_at"])
            self.assertEqual(cli("inventory", "list", "--json")["items"], [])
            self.assertEqual(cli("inventory", "list", "--include-archived", "--json")["items"], [archived])
            self.assertIn("CLI-1", cli("inventory", "list", "--include-archived").stdout)
            cli("inventory", "restore", item, "--revision", "2", "--key", "stale", code=7)
        with workspace_client(self.workspace) as (client, _):
            restore = ("inventory", "restore", item, "--revision", "3", "--key", "restore", "--json")
            restored = cli(*restore)
            self.assertEqual(cli(*restore), restored)
            self.assertEqual(cli("inventory", "list", "--json")["items"], [restored])
            self.assertEqual((restored["archived_at"], restored["revision"], restored["sku"]), (None, 4, "CLI-1"))
        self.assertEqual(self.snapshot("SELECT source, COUNT(*) FROM audit_events "
                                       "WHERE entity_type='inventory_item' GROUP BY source"), [("cli", 4)])

    def test_web_restore_api_replay_and_refusals(self):
        self.add_user("viewer", "viewer")
        self.add_user("other", "admin", shop_id=2)
        with closing(sqlite3.connect(self.db)) as db, db:
            outside = db.execute(
                "INSERT INTO inventory_items(shop_id,sku,description,unit,archived_at) "
                "VALUES (2,'RC-1','Outside part','pcs','archived')"
            ).lastrowid

        def state():
            catalog = tuple(self.snapshot(f"SELECT * FROM {table} ORDER BY rowid") for table in
                            ("inventory_items", "inventory_requests", "inventory_movements"))
            audit = self.snapshot("SELECT * FROM audit_events WHERE entity_type='inventory_item' ORDER BY id")
            return catalog, audit

        with self.client() as client:
            auth = self.token(client)
            item_id = client.post(API, headers=auth | {"Idempotency-Key": "create"}, json=ITEM).json()["id"]
            api_path = f"{API}/{item_id}"
            web_path = f"/inventory/{item_id}"
            self.assertEqual(client.post(api_path + "/archive", headers=auth | {"Idempotency-Key": "archive"},
                                         json={"revision": 1}).status_code, 200)
            self.assertIn("Restore inventory item", client.get(web_path).text)
            archived = state()
            client.cookies.clear()
            viewer = self.token(client, "viewer", scope="read")
            self.assertNotIn("Restore inventory item", client.get(web_path).text)
            for path, headers, status in (
                (api_path, viewer, 403), (api_path, self.direct_token("other"), 401),
                (f"{API}/{outside}", auth, 404),
            ):
                # The foreign record has revision 1 and would otherwise be restorable.
                revision = 1 if status == 404 else 2
                response = client.post(path + "/restore", headers=headers | {"Idempotency-Key": "denied"},
                                       json={"revision": revision})
                self.assertEqual(response.status_code, status)
            client.cookies.clear()
            self.sign_in(client)
            detail = client.get(web_path)
            fields = {name: hidden(detail, name) for name in ("csrf", "key", "revision")}
            for overrides, status in (({"csrf": "bad"}, 403), ({"revision": "1"}, 409), ({"revision": ""}, 422)):
                response = client.post(web_path + "/restore", data=fields | overrides)
                self.assertEqual(response.status_code, status)
            self.assertEqual(state(), archived)

            self.assertEqual(client.post(web_path + "/restore", data=fields).status_code, 303)
            restored = client.get(api_path, headers=auth).json()
            self.assertEqual((restored["sku"], restored["revision"], restored["archived_at"]), ("RC-1", 3, None))
            after = state()
            self.assertEqual(client.post(web_path + "/restore", data=fields).status_code, 303)
            replay_headers = auth | {"Idempotency-Key": fields["key"]}
            self.assertEqual(client.post(api_path + "/restore", headers=replay_headers,
                                         json={"revision": 2}).json(), restored)
            for key, revision, code in ((fields["key"], 3, "conflict"), ("new-restore", 3, "same_state")):
                response = client.post(api_path + "/restore", headers=auth | {"Idempotency-Key": key},
                                       json={"revision": revision})
                self.assertEqual(response.json()["error"]["code"], code)
            self.assertEqual(state(), after)

        with self.client() as restarted:
            self.assertEqual(restarted.get(API, headers=auth).json()["items"], [restored])
        self.assertEqual(self.snapshot(
            "SELECT actor_user_id, source, operation FROM audit_events WHERE operation='inventory.restore'"
        ), [(1, "web", "inventory.restore")])



class StockAdapterTests(InventoryAdapterTests):
    test_api_web_parity_redaction_refusals_and_restart = None
    test_real_cli_create_replay_edit_archive_and_list = None
    test_web_restore_api_replay_and_refusals = None

    def test_api_and_web_receive_adjust_reverse_and_reconcile(self):
        self.add_user("viewer", "viewer")
        with self.client() as client:
            auth = self.token(client)
            item = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=ITEM).json()
            path = f"{API}/{item['id']}"

            def post(suffix, body, key=None, headers=auth):
                return client.post(path + suffix, headers={**headers, "Idempotency-Key": key or uuid4().hex},
                                   json=body)

            key = uuid4().hex
            received = post("/receive", {"quantity_milli": 4000, "revision": 1, "reason": "Delivery"}, key)
            self.assertEqual(received.status_code, 201, received.text)
            change = received.json()
            self.assertEqual((change["item"]["on_hand_milli"], change["item"]["revision"],
                              change["movement"]["kind"]), (4000, 2, "receive"))
            self.assertEqual(post("/receive", {"quantity_milli": 4000, "revision": 1, "reason": "Delivery"},
                                  key).json(), change)
            for suffix, body, code in (
                    ("/receive", {"quantity_milli": 1, "revision": 1}, "stale_revision"),
                    ("/adjust", {"quantity_milli": -4001, "reason": "Count", "revision": 2}, "insufficient_stock"),
                    ("/adjust", {"quantity_milli": -1, "revision": 2}, "validation"),
                    ("/adjust", {"quantity_milli": -1, "reason": "x", "revision": 2, "shop_id": 2}, "validation"),
                    ("/receive", {"quantity_milli": 0.5, "revision": 2}, "validation")):
                self.assertEqual(post(suffix, body).json()["error"]["code"], code, (suffix, body))
            self.assertEqual(client.patch(path, headers={**auth, "Idempotency-Key": uuid4().hex},
                                          json={**ITEM, "unit": "m", "revision": 2}).json()["error"]["code"],
                             "unit_locked")

            detail = client.get(f"/inventory/{item['id']}")
            self.assertIn("On hand", detail.text)
            self.assertIn("4 pcs", detail.text)
            form = {"csrf": hidden(detail, "csrf"), "revision": hidden(detail, "revision")}
            adjust = {**form, "key": uuid4().hex, "direction": "remove", "quantity": "1.5", "reason": ""}
            invalid = client.post(f"/inventory/{item['id']}/adjust", data=adjust)
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('value="1.5"', invalid.text)
            self.assertEqual(client.post(f"/inventory/{item['id']}/adjust",
                                         data={**adjust, "csrf": "bad", "reason": "Lost"}).status_code, 403)
            adjust["reason"] = "Lost"
            self.assertEqual(client.post(f"/inventory/{item['id']}/adjust", data=adjust).status_code, 303)
            self.assertEqual(client.post(f"/inventory/{item['id']}/adjust", data=adjust).status_code, 303)
            stale = client.post(f"/inventory/{item['id']}/receive",
                                data={**form, "key": uuid4().hex, "quantity": "1", "reason": ""})
            self.assertEqual(stale.status_code, 409)
            detail = client.get(f"/inventory/{item['id']}")
            self.assertIn("2.5 pcs", detail.text)
            movements = client.get(f"{path}/movements", headers=auth).json()["movements"]
            self.assertEqual([(m["kind"], m["quantity_milli"]) for m in movements],
                             [("adjust", -1500), ("receive", 4000)])
            reverse = {"csrf": hidden(detail, "csrf"), "revision": hidden(detail, "revision"),
                       "key": uuid4().hex, "reason": "Found it"}
            self.assertEqual(client.post(f"/inventory/{item['id']}/movements/{movements[0]['id']}/reverse",
                                         data=reverse).status_code, 303)
            self.assertEqual(client.post(f"{path}/receive", headers={**auth, "Idempotency-Key": uuid4().hex},
                                         json={"quantity_milli": 500, "revision": 4}).status_code, 201)
            reversal = post(f"/movements/{change['movement']['id']}/reverse",
                            {"reason": "Wrong delivery", "revision": 5})
            self.assertEqual((reversal.status_code, reversal.json()["item"]["on_hand_milli"]), (201, 500))
            self.assertEqual(client.get(f"{API}/reconciliation", headers=auth).json(),
                             {"items_checked": 1, "mismatches": [], "issues": []})

        with self.client() as client:
            viewer = self.token(client, "viewer", scope="read")
            self.assertEqual(client.get(path, headers=viewer).json()["on_hand_milli"], 500)
            self.assertEqual(len(client.get(f"{path}/movements", headers=viewer).json()["movements"]), 5)
            self.assertEqual(post("/receive", {"quantity_milli": 1, "revision": 6}, headers=viewer).status_code, 403)
            page = client.get(f"/inventory/{item['id']}")
            self.assertIn("0.5 pcs", page.text)
            self.assertNotIn('name="quantity"', page.text)
            self.assertIn("0.5", client.get("/inventory").text)
        self.assertEqual(self.snapshot(
            "SELECT operation, source FROM audit_events WHERE operation IN "
            "('inventory.receive','inventory.adjust','inventory.reverse') ORDER BY id"), [
            ("inventory.receive", "api"), ("inventory.adjust", "web"), ("inventory.reverse", "web"),
            ("inventory.receive", "api"), ("inventory.reverse", "api")])

    def test_real_cli_receive_adjust_reverse_movements_and_reconcile(self):
        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))

            def cli(*args, code=0, stdin=None):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if "--json" in args and code == 0 else result

            item = str(cli("inventory", "create", "--sku", "CLI-S", "--description", "Stocked",
                           "--key", uuid4().hex, "--json")["id"])
            receive = ("inventory", "receive", item, "--quantity", "3.25", "--revision", "1",
                       "--reason", "Box 4", "--key", uuid4().hex, "--json")
            received = cli(*receive)
            self.assertEqual(cli(*receive), received)
            self.assertEqual(received["item"]["on_hand_milli"], 3250)
            cli("inventory", "adjust", item, "--quantity", "-4", "--reason", "Count", "--revision", "2",
                "--key", uuid4().hex, code=7)
            adjusted = cli("inventory", "adjust", item, "--quantity", "-0.25", "--reason", "Count",
                           "--revision", "2", "--key", uuid4().hex, "--json")
            self.assertEqual(adjusted["item"]["on_hand_milli"], 3000)
            reversed_ = cli("inventory", "reverse", item, str(adjusted["movement"]["id"]), "--reason", "Recount",
                            "--revision", "3", "--key", uuid4().hex, "--json")
            self.assertEqual(reversed_["item"]["on_hand_milli"], 3250)
            self.assertIn("On hand\t3.25 pcs", cli("inventory", "show", item).stdout)
            listing = cli("inventory", "movements", item).stdout
            self.assertEqual([line.split("\t")[1:3] for line in listing.splitlines()],
                             [["reversal", "0.25"], ["adjust", "-0.25"], ["receive", "3.25"]])
            self.assertIn("1 items checked, 0 mismatches", cli("inventory", "reconcile").stdout)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE inventory_items SET on_hand_milli = 1")
        with workspace_client(self.workspace) as (client, _):
            self.assertIn(f"Mismatch\t{item}\tCLI-S\tcached 0.001\tledger 3.25",
                          cli("inventory", "reconcile", code=1).stdout)
