"""S3 search and filtered pagination for repairs and customers across web, API and CLI."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from repairlib.search import fold, like_pattern
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret

CUSTOMERS = "/api/v1/customers"


class FoldTests(unittest.TestCase):
    def test_case_and_accent_folding_with_literal_wildcards(self):
        self.assertEqual(fold("Πελάτης ΔΟΚΙΜΉΣ"), fold("πελατης δοκιμης"))
        self.assertEqual(fold("Ünïcode"), "unicode")
        self.assertEqual(like_pattern("50%_off\\"), "%50\\%\\_off\\\\%")


class SearchTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def seed(self, client, auth):
        customers = {}
        for name, phone in (("Πελάτης Δοκιμής", "+357 555 0102"), ("Synthetic Alpha", "+357 555 0200"),
                            ("100% Synthetic", "+357 555 0300")):
            reply = client.post(CUSTOMERS, headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"name": name, "phone": phone})
            customers[name] = reply.json()["id"]
        repairs = []
        for index in range(30):
            details = {**DEVICE, "model": f"Model {index:02d}", "serial": f"SN-{index:03d}",
                       "customer_id": customers["Πελάτης Δοκιμής"] if index % 3 == 0 else None}
            if index == 7:
                details["reported_issue"] = "Cracked ΟΘΌΝΗ after drop"
            repairs.append(client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                       json=details).json())
        client.post(f"{API}/{repairs[4]['id']}/stage", headers={**auth, "Idempotency-Key": uuid4().hex},
                    json={"revision": 1, "stage": "waiting_parts"})
        return customers, repairs

    def test_api_search_filters_pagination_and_second_shop_isolation(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            customers, repairs = self.seed(client, auth)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO customers(id,shop_id,name) VALUES (90,2,'Πελάτης Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue,model) VALUES (90,2,'οθόνη','Model 07')")

            found = client.get(CUSTOMERS, params={"q": "πελατης"}, headers=auth).json()
            self.assertEqual([item["id"] for item in found["items"]], [customers["Πελάτης Δοκιμής"]])
            self.assertEqual([item["name"] for item in client.get(CUSTOMERS, params={"q": "0200"},
                                                                  headers=auth).json()["items"]],
                             ["Synthetic Alpha"])
            literal = client.get(CUSTOMERS, params={"q": "100%"}, headers=auth).json()["items"]
            self.assertEqual([item["name"] for item in literal], ["100% Synthetic"])

            screen = client.get(API, params={"q": "οθονη"}, headers=auth).json()["items"]
            self.assertEqual([item["id"] for item in screen], [repairs[7]["id"]])
            # Every word may match a different field, in any order: "Anna iPad", "Giorgos Dell XPS".
            mixed = client.get(API, params={"q": "06 δοκιμης  model"}, headers=auth).json()["items"]
            self.assertEqual([item["id"] for item in mixed], [repairs[6]["id"]])
            self.assertEqual(client.get(API, params={"q": "δοκιμης Model 07"}, headers=auth).json()["items"], [])
            by_customer = client.get(API, params={"q": "ΔΟΚΙΜΗΣ", "limit": "4"}, headers=auth).json()
            self.assertEqual(len(by_customer["items"]), 4)
            second = client.get(API, params={"q": "ΔΟΚΙΜΗΣ", "limit": "4",
                                             "before": str(by_customer["next_before"])}, headers=auth).json()
            ids = [item["id"] for item in by_customer["items"] + second["items"]]
            self.assertEqual(ids, sorted((r["id"] for r in repairs[::3]), reverse=True)[:8])
            waiting = client.get(API, params={"stage": "waiting_parts"}, headers=auth).json()["items"]
            self.assertEqual([item["id"] for item in waiting], [repairs[4]["id"]])
            self.assertEqual(client.get(API, params={"q": "SN-004", "stage": "new"}, headers=auth)
                             .json()["items"], [])
            self.assertEqual(client.get(API, params={"stage": "lost"}, headers=auth).status_code, 422)
            self.assertEqual(client.get(API, params={"q": "x" * 101}, headers=auth).status_code, 422)
            self.assertNotIn("Outside", client.get(CUSTOMERS, params={"q": "Outside"}, headers=auth).text)
            self.assertNotIn('"id":90', client.get(API, params={"q": "Model 07"}, headers=auth).text)

    def test_browser_search_forms_keep_filters_across_pages_without_javascript(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            _, repairs = self.seed(client, auth)
            page = client.get("/repairs", params={"q": "Synthetic", "stage": "new"})
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="q" value="Synthetic"', page.text)
            self.assertIn('<option value="new" selected>', page.text)
            self.assertIn("q=Synthetic", page.text)
            self.assertIn("stage=new", page.text)
            self.assertIn("before=", page.text)
            screen = client.get("/repairs", params={"q": "ΟΘΟΝΗ"})
            self.assertIn(f"#{repairs[7]['id']}:", screen.text)
            self.assertNotIn(f"#{repairs[8]['id']}:", screen.text)
            customers = client.get("/customers", params={"q": "δοκιμής"})
            self.assertIn("Πελάτης Δοκιμής", customers.text)
            self.assertNotIn("Synthetic Alpha", customers.text)

    def test_real_http_cli_search(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            _, repairs = self.seed(client, auth)

            def cli(*args):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace), "--json"],
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            self.assertEqual([item["id"] for item in cli("repairs", "list", "--query", "οθόνη")["items"]],
                             [repairs[7]["id"]])
            waiting = cli("repairs", "list", "--stage", "waiting_parts", "--queue", "open",
                          "--no-picked-up", "--no-archived", "--technician", "unassigned")
            self.assertEqual([item["id"] for item in waiting["items"]], [repairs[4]["id"]])
            self.assertEqual(waiting["total"], 1)
            self.assertEqual([item["name"] for item in cli("customers", "list", "--query", "alpha")["items"]],
                             ["Synthetic Alpha"])


if __name__ == "__main__":
    unittest.main()
