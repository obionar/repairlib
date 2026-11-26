"""UI-3 register filters, queue links and shop-scoped list projections."""

from contextlib import closing
from html import unescape
import re
import sqlite3
from html.parser import HTMLParser
import unittest

from test_dashboard import links, queue, seed_dashboard
from test_tokens import ACCOUNT, TokenFixture, issued_secret


def seed_registers(path):
    seed_dashboard(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("UPDATE repairs SET customer_id = 7 WHERE id IN (6, 10)")
        db.execute("UPDATE repairs SET display_device = 'Bench notebook' WHERE id = 1")
        db.execute("UPDATE customers SET name = 'Synthetic workshop customer with a long contact name' WHERE id = 6")
        db.executemany(
            """INSERT INTO repairs(id, shop_id, stage, reported_issue, picked_up_at, archived_at)
               VALUES (?, 1, 'repaired', 'Batch screen repair', NULL, NULL)""",
            [(i,) for i in range(30, 57)],
        )
        db.executemany(
            """INSERT INTO inventory_items
               (id, shop_id, sku, description, unit, category, manufacturer, part_number, purchase_cost_cents)
               VALUES (?, ?, ?, ?, 'pcs', 'Οθόνες', ?, ?, 1847)""",
            [(i, 1, f"SCREEN-{i:02}", "Display module", "Δοκιμή", "PN_42") for i in range(1, 28)]
            + [(41, 1, "OTHER", "Display module", "Δοκιμή", "PNX42"),
               (42, 2, "OUTSIDE", "Display module", "Δοκιμή", "PN_42")],
        )


class WebListsTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def auth(self, client, username="owner"):
        self.sign_in(client, username)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client, scope="read")))
        return {"Authorization": f"Bearer {token}"}

    def test_repair_list_filters_and_queue_links(self):
        seed_registers(self.db)
        with self.client() as client:
            auth = self.auth(client, "tech")

            def listing(**params):
                result = client.get("/api/v1/repairs", headers=auth, params=params)
                self.assertEqual(result.status_code, 200)
                return result.json()

            self.assertEqual([row["id"] for row in listing(picked_up=True, technician="me")["items"]], [6])
            self.assertEqual([row["id"] for row in listing(archived=True)["items"]], [14, 10])
            self.assertEqual([row["id"] for row in listing(technician="unassigned", customer_id=7, archived=False)["items"]], [11])
            self.assertEqual([row["id"] for row in listing(technician="2")["items"]], [6, 1])
            self.assertEqual(listing(technician="4", shop_id=2)["items"], [])
            self.assertEqual(listing(customer_id=9)["items"], [])
            for section, key, count, last_id in (
                ("open-work", "open", 9, 2), ("awaiting-pickup", "pickup", 36, 56),
            ):
                panel = queue(client.get("/").text, section)
                self.assertIn(f'href="/repairs?queue={key}"', panel)
                self.assertNotIn(last_id, links(panel, "repairs"))
                api = listing(queue=key, limit=100)
                self.assertEqual(api["total"], count)
                self.assertIn(last_id, [row["id"] for row in api["items"]])
                web = client.get(f"/repairs?queue={key}")
                self.assertEqual(links(web.text, "repairs"), [row["id"] for row in api["items"][:25]])
            self.assertEqual(listing(queue="pickup", before=1)["total"], 36)
            self.assertEqual(listing(queue="pickup", picked_up=True)["total"], 0)
            for filters in ("queue=bogus", "technician=bogus", "customer_id=0"):
                self.assertEqual(client.get(f"/api/v1/repairs?{filters}", headers=auth).status_code, 422)
                self.assertEqual(client.get(f"/repairs?{filters}").status_code, 422)

    def test_payment_filter_combines_with_pickup_in_every_adapter(self):
        seed_registers(self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE repairs SET payment_paid = 1 WHERE id IN (6, 30)")
            db.execute("UPDATE repairs SET payment_paid = 0 WHERE id = 31")
            db.execute("UPDATE repairs SET payment_paid = 1, picked_up_at = '2026-01-01T00:00:00.000Z' WHERE shop_id = 2")
            picked = [row[0] for row in db.execute(
                "SELECT id FROM repairs WHERE shop_id = 1 AND picked_up_at IS NOT NULL ORDER BY id DESC")]
        self.assertIn(6, picked)
        with self.client() as client:
            auth = self.auth(client)

            def ids(**params):
                result = client.get("/api/v1/repairs", headers=auth, params={**params, "limit": 100})
                self.assertEqual(result.status_code, 200, result.text)
                return [row["id"] for row in result.json()["items"]]

            # "Not paid" covers both recorded unpaid and no payment recorded; other shops never appear.
            self.assertEqual(ids(paid=True), [30, 6])
            self.assertEqual(ids(picked_up=True, paid=False), [i for i in picked if i != 6])
            self.assertIn(31, ids(paid=False))
            self.assertNotIn(30, ids(paid=False))
            web = client.get("/repairs?picked_up=true&paid=false")
            self.assertEqual(links(web.text, "repairs"), [i for i in picked if i != 6][:25])
            self.assertIn('<option value="false" selected>', web.text.split('id="repair-paid"', 1)[1].split("</select>", 1)[0])
            self.assertEqual(client.get("/repairs?paid=bogus").status_code, 422)

    def test_repair_rows_show_customer_status_and_date_without_technician(self):
        seed_registers(self.db)
        with self.client() as client:
            auth = self.auth(client)
            result = client.get("/api/v1/repairs", headers=auth, params={"q": "Bench notebook"}).json()
            row, = result["items"]
            self.assertEqual((row["customer_name"], row["technician_username"]), ("Synthetic customer 7", "tech"))
            page = client.get("/repairs?q=Bench+notebook")
            self.assertEqual(links(page.text, "repairs"), [1])
            self.assertEqual(links(page.text, "customers"), [7])
            for value in ("Bench notebook", "Synthetic customer 7", "New"):
                self.assertIn(value, page.text)
            table = page.text.split("<table", 1)[1].split("</table>", 1)[0]
            self.assertNotIn(">Technician<", table)
            self.assertNotIn(">tech<", table)
            self.assertRegex(table, r'<time datetime="[^"]+">\d{4}-\d{2}-\d{2}</time>')
            self.assertIn(">Archived</span>", client.get("/repairs?q=Archived+open").text)
            self.assertIn(">Picked up</span>", client.get("/repairs?q=Early+pickup").text)

    def test_filters_persist_across_pages(self):
        seed_registers(self.db)
        with self.client() as client:
            self.sign_in(client)
            params = "q=Batch&stage=repaired&picked_up=false&archived=false&technician=unassigned&queue=pickup"
            first = client.get("/repairs?" + params)
            older = unescape(re.search(r'href="([^"]+)">Older repairs</a>', first.text)[1])
            self.assertTrue(set(params.split("&")) <= set(older[1:].split("&")))
            second = client.get("/repairs" + older)
            self.assertEqual(links(first.text, "repairs") + links(second.text, "repairs"),
                             list(range(56, 29, -1)))
            newest = unescape(re.search(r'href="([^"]+)">Newest repairs</a>', second.text)[1])
            self.assertEqual(links(client.get(newest).text, "repairs"), links(first.text, "repairs"))

    def test_inventory_search_and_viewer_list_hides_costs(self):
        seed_registers(self.db)
        with self.client() as client:
            auth = self.auth(client, "reader")
            for query in ("SCREEN", "Display module", "δοκιμη", "PN_42"):
                api = client.get("/api/v1/inventory", headers=auth,
                                 params={"q": query, "category": "οθονες", "limit": 100}).json()
                expected = [41, *range(27, 0, -1)] if query in ("Display module", "δοκιμη") else list(range(27, 0, -1))
                self.assertEqual([item["id"] for item in api["items"]], expected)
                self.assertTrue(all(item["purchase_cost_cents"] is None for item in api["items"]))
            first = client.get("/inventory", params={"q": "PN_42", "category": "Οθόνες", "low_stock": True})
            self.assertNotIn("18.47", first.text)
            self.assertNotIn("Purchase cost", first.text)
            older = unescape(re.search(r'href="([^"]+)">Older items</a>', first.text)[1])
            self.assertIn("q=PN_42", older)
            self.assertIn("category=", older)
            self.assertEqual(links(first.text, "inventory") + links(client.get("/inventory" + older).text, "inventory"),
                             list(range(27, 0, -1)))
            self.assertEqual(client.get("/inventory/42").status_code, 404)

    def test_inventory_filter_controls_belong_to_a_form(self):
        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.current = None
                self.forms = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.current = set()
                    self.forms.append(self.current)
                elif self.current is not None and tag in ("input", "button") and attrs.get("name"):
                    self.current.add(attrs["name"])

            def handle_endtag(self, tag):
                if tag == "form":
                    self.current = None

        with self.client() as client:
            self.sign_in(client)
            parser = Forms()
            parser.feed(client.get("/inventory?q=screen&category=Parts").text)
            self.assertTrue(any({"q", "category", "include_archived", "low_stock"} <= form
                                for form in parser.forms))
            self.assertTrue(any({"q", "category"} <= form for form in parser.forms))

    def test_item_sku_is_not_translated(self):
        from repairlib.client import local
        ctx = local.open_workspace(self.workspace)
        item = local.create_inventory_item(ctx, {"sku": "Inventory", "description": "Part",
                                                  "selling_price_cents": 100}, "sku")
        with self.client() as client:
            self.sign_in(client)
            page = client.get(f"/inventory/{item.id}", headers={"Accept-Language": "el"})
            self.assertEqual(page.status_code, 200)
            self.assertIn('<h2 id="inventory-item">Inventory</h2>', page.text)

    def test_customer_open_counts(self):
        seed_registers(self.db)
        with self.client() as client:
            auth = self.auth(client)
            result = client.get("/api/v1/customers", headers=auth,
                                params={"q": "Synthetic customer 7", "shop_id": 2}).json()
            customer, = result["items"]
            self.assertEqual(customer["open_repair_count"], 2)
            self.assertEqual(client.get("/api/v1/customers", headers=auth,
                                        params={"q": "Outside"}).json()["items"], [])
            page = client.get("/customers?q=Synthetic+customer+7")
            self.assertIn("Open repairs", page.text)
            self.assertRegex(page.text, r'<td[^>]*>2</td>')
