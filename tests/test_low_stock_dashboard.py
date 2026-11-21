"""Low-stock dashboard and inventory filters through authenticated adapters."""

from contextlib import closing
from html import unescape
import json
import re
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_dashboard import links, queue
from test_tokens import ACCOUNT, TokenFixture, issued_secret

FILTER = "/inventory?low_stock=true"
API = "/api/v1/inventory"
LOW_IDS = list(range(27, 0, -1))


def seed_low_stock(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
        db.executemany(
            """INSERT INTO users(shop_id, username, role, password_hash)
               SELECT 1, ?, ?, password_hash FROM users WHERE username = 'owner'""",
            [("tech", "technician"), ("reader", "viewer")],
        )
        db.executemany(
            """INSERT INTO inventory_items(id, shop_id, sku, description, unit, on_hand_milli,
                                           low_stock_milli, archived_at, purchase_cost_cents)
               VALUES (?, ?, ?, 'Synthetic part', 'pcs', ?, ?, ?, 1847)""",
            [(i, 1, f"PART-{i:02}", 500, 1000, None) for i in range(1, 24)]
            + [
                (24, 1, "ZERO-POSITIVE", 0, 1000, None),
                (25, 1, "ZERO-ZERO", 0, 0, None),
                (26, 1, "AT-THRESHOLD", 1000, 1000, None),
                (27, 1, "NEGATIVE", 0, 0, None),
                (28, 1, "ABOVE", 1001, 1000, None),
                (29, 1, "ARCHIVED", 0, 1000, "2026-11-18"),
                (30, 2, "OUTSIDE", 0, 1000, None),
            ],
        )
        # Supported writes cannot produce negative stock. Exercise a damaged cache.
        db.execute("PRAGMA ignore_check_constraints = ON")
        db.execute("UPDATE inventory_items SET on_hand_milli = -1 WHERE id = 27")


class LowStockDashboardTests(TokenFixture, unittest.TestCase):
    def test_empty_and_live_stock_changes_after_restart(self):
        with self.client() as client:
            self.assertEqual(client.get(FILTER).headers.get("location"), "/login")
            self.assertEqual(client.get(f"{API}?low_stock=true").status_code, 401)
            self.sign_in(client)
            empty = queue(client.get("/").text, "low-stock")
            self.assertIn("0 low-stock items", empty)
            self.assertIn("No low-stock items.", empty)
            self.assertIn(f'href="{FILTER}"', empty)
            self.assertEqual(links(client.get(FILTER).text, "inventory"), [])
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            made = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"sku": "FUSE", "description": "Fuse", "unit": "pcs",
                                     "low_stock_milli": 1000})
            self.assertEqual(made.status_code, 201)
            item_id = made.json()["id"]
            self.assertEqual(links(queue(client.get("/").text, "low-stock"), "inventory"), [item_id])
            path = f"{API}/{item_id}"
            refused = client.post(path + "/adjust", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"quantity_milli": -1, "reason": "Count", "revision": 1})
            self.assertEqual(refused.json()["error"]["code"], "insufficient_stock")
            self.assertEqual(client.get(path, headers=auth).json()["revision"], 1)
            received = client.post(path + "/receive", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"quantity_milli": 1001, "revision": 1})
            self.assertEqual(received.status_code, 201)

        with self.client() as client:
            self.sign_in(client)
            self.assertIn("0 low-stock items", queue(client.get("/").text, "low-stock"))
            self.assertEqual(links(client.get(FILTER).text, "inventory"), [])
            self.assertEqual(links(client.get("/inventory").text, "inventory"), [item_id])

    def test_dashboard_and_paginated_inventory_agree_for_all_roles(self):
        seed_low_stock(self.db)
        for username in ("owner", "tech", "reader"):
            with self.subTest(username=username), self.client() as client:
                self.sign_in(client, username)
                preview = queue(client.get("/?shop_id=2").text, "low-stock")
                self.assertIn("27 low-stock items", preview)
                self.assertIn(f'href="{FILTER}"', preview)
                self.assertEqual(links(preview, "inventory"), LOW_IDS[:8])
                self.assertIn("-0.001 pcs", preview)
                self.assertNotIn("18.47", preview)

                first = client.get(FILTER + "&include_archived=true&shop_id=2")
                self.assertEqual(first.status_code, 200)
                self.assertRegex(first.text, r'name="low_stock" value="true" checked')
                older = re.search(r'href="([^"]+)">Older items</a>', first.text)[1]
                second = client.get("/inventory" + unescape(older))
                self.assertEqual(links(first.text, "inventory") + links(second.text, "inventory"), LOW_IDS)
                newest = re.search(r'href="([^"]+)">Newest items</a>', second.text)[1]
                self.assertEqual(links(client.get(unescape(newest)).text, "inventory"),
                                 LOW_IDS[:25])
                self.assertEqual("18.47" in first.text, username != "reader")
                self.assertEqual(client.get("/inventory/30").status_code, 404)

                token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
                auth = {"Authorization": f"Bearer {token}"}
                result = client.get(API, headers=auth, params={
                    "low_stock": "true", "include_archived": "true", "limit": 100, "shop_id": 2,
                })
                self.assertEqual(result.status_code, 200)
                items = result.json()["items"]
                self.assertEqual([item["id"] for item in items], LOW_IDS)
                self.assertTrue(all(item["purchase_cost_cents"] == (None if username == "reader" else 1847)
                                    for item in items))

    def test_http_cli_low_stock_filter_and_cursor(self):
        seed_low_stock(self.db)
        with workspace_client(self.workspace) as (client, _):
            self.sign_in(client, "reader")
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))

            def cli(*args, stdin=None):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)], input=stdin,
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout) if "--json" in args else None

            args = ("inventory", "list", "--query", "synthetic part", "--low-stock",
                    "--include-archived", "--json", "--limit", "25")
            first = cli(*args)
            second = cli(*args, "--before", str(first["next_before"]))
            items = first["items"] + second["items"]
            self.assertEqual([item["id"] for item in items], LOW_IDS)
            # The local CLI acts as a shop admin, so purchase costs are visible.
            self.assertTrue(all(item["purchase_cost_cents"] is not None for item in items))
            self.assertIsNone(second["next_before"])
