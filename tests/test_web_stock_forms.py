"""UI-5 stock picker and conflict review through native forms."""

from contextlib import closing
import re
import sqlite3
import unittest
from uuid import uuid4

from test_inventory_adapters import API as INVENTORY, ITEM
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_feedback import form_for


class WebStockFormsTests(TokenFixture, unittest.TestCase):
    def test_manual_part_without_inventory_is_billed_and_logged_once(self):
        with self.client() as client:
            auth = self.auth(client)
            repair = self.post(client, API, auth, DEVICE)
            detail = f"/repairs/{repair['id']}"
            action = detail + "/consume-part"
            ticket = client.get(detail)
            parts = ticket.text.split('aria-labelledby="stock-usage">', 1)[1].split('</section>', 1)[0]
            self.assertIn('id="manual-description"', parts)
            form = form_for(ticket, action)
            data = form | {"description": "1k resistor R0805", "quantity": "1", "unit_price": "0.1"}
            self.assertEqual(client.post(action, data=data | {"csrf": "bad"}).status_code, 403)
            invalid = client.post(action, data=data | {"quantity": "0"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('aria-labelledby="stock-usage"', invalid.text)
            self.assertIn('value="1k resistor R0805"', invalid.text)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM repair_charges"), [(0,)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM work_entries"), [(0,)])
            self.assertEqual(client.post(action, data=data).status_code, 303)
            self.assertEqual(client.post(action, data=data).status_code, 303)
            self.assertEqual(client.post(action, data=data | {"unit_price": "0.2"}).status_code, 409)
            page = client.get(detail).text
            used = page.split('aria-labelledby="stock-usage">', 1)[1].split('</section>', 1)[0]
            self.assertIn("1k resistor R0805", used)
            self.assertIn("0.10 €", used)
            self.assertEqual(page.count('<p class="obi-entry-body">Part used: 1k resistor R0805 × 1 pcs</p>'), 1)
            self.assertEqual(self.snapshot("SELECT quantity_milli, unit_price_cents FROM repair_charges"), [(1000, 10)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM work_entries"), [(1,)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM inventory_movements"), [(0,)])

    def auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def post(self, client, path, auth, payload, status=201):
        response = client.post(path, headers=auth | {"Idempotency-Key": uuid4().hex}, json=payload)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def seed_picker(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops VALUES (2, 'Outside shop')")
            db.executemany("""INSERT INTO inventory_items(id, shop_id, sku, description, unit, archived_at)
                              VALUES (?, ?, ?, ?, 'pcs', ?)""",
                           [(i, 1, f"P-{i}", "Φίλτρο", None) for i in range(1, 29)]
                           + [(29, 1, "ARCHIVE", "Φίλτρο", "2026-01-01"),
                              (30, 2, "OUTSIDE", "Φίλτρο", None), (31, 1, "OTHER", "Adapter", None)])
            db.execute("""INSERT INTO users(shop_id, username, role, password_hash)
                          SELECT 1, 'reader', 'viewer', password_hash FROM users WHERE id=1""")

    def test_search_paging_and_reviewed_consumption(self):
        self.seed_picker()
        with self.client() as client, self.client() as viewer:
            auth = self.auth(client)
            part = self.post(client, f"{INVENTORY}/1/receive", auth, {"revision": 1, "quantity_milli": 4000})
            repair = self.post(client, API, auth, DEVICE)
            route = f"/repairs/{repair['id']}/consume-part"
            first = client.get(route, params={"q": "φιλτρο"})
            self.assertIn('name="q"', first.text)
            for unwanted in ("OUTSIDE", "ARCHIVE", "OTHER", f'href="{route}/1?'):
                self.assertNotIn(unwanted, first.text)
            older = re.search(r'href="([^\"]*before=\d+)"[^>]*>Older items</a>', first.text)[1].replace("&amp;", "&")
            self.assertIn("q=", older)
            page = client.get(route + older)
            review_url = re.search(rf'href="({route}/1\?[^\"]+)"', page.text)[1].replace("&amp;", "&")
            review = client.get(review_url)
            self.assertIn("4 pcs", review.text)
            self.assertIn(f'{route}{older.replace("&", "&amp;")}', review.text)
            self.assertEqual(client.get(route + "/30").status_code, 404)
            self.sign_in(viewer, "reader")
            self.assertEqual(viewer.get(route, params={"q": "φιλτρο"}).status_code, 403)
            self.assertEqual(viewer.get(review_url).status_code, 403)

            form = form_for(review, review_url.replace("&", "&amp;"))
            self.assertEqual(int(form["item_revision"]), part["item"]["revision"])
            consumed = {**form, "consumed_quantity": "1.5", "billed_quantity": "1", "unit_price": "3.25"}
            invalid = client.post(review_url, data={**consumed, "billed_quantity": "invalid"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn(f'{route}{older.replace("&", "&amp;")}', invalid.text)
            self.assertEqual(client.post(review_url, data=consumed).status_code, 303)
            self.assertEqual(client.post(review_url, data=consumed).status_code, 303)
            ticket = client.get(f"/repairs/{repair['id']}").text
            used = ticket.split('aria-labelledby="stock-usage">', 1)[1].split('</section>', 1)[0]
            self.assertIn("Φίλτρο · ×1.5 pcs · 3.25 €", used)
            self.assertIn('<p class="obi-entry-body">Part used: Φίλτρο × 1.5 pcs</p>', ticket)
            greek = client.get(f"/repairs/{repair['id']}", cookies={"repairlib_lang": "el"}).text
            used = greek.split('aria-labelledby="stock-usage">', 1)[1].split('</section>', 1)[0]
            self.assertIn("Φίλτρο · ×1.5 τεμ. · 3.25 €", used)  # stored "pcs" shown in the reader's language
        with self.client() as restarted:
            self.assertEqual(restarted.get(f"{INVENTORY}/1", headers=auth).json()["on_hand_milli"], 2500)
            charges = restarted.get(f"{API}/{repair['id']}/charges", headers=auth).json()
            self.assertEqual(charges["total_cents"], 325)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='inventory.consume'"), [(1,)])

    def seed_ledger(self, client, auth):
        item_id = self.post(client, INVENTORY, auth, ITEM)["id"]
        route = f"{INVENTORY}/{item_id}/receive"
        for revision in range(1, 28):
            self.post(client, route, auth, {"revision": revision, "quantity_milli": 1000,
                                            "reason": "Original receipt" if revision == 1 else "Delivery"})
        return item_id

    def test_stale_reversal_preserves_older_movement_and_reason_for_review(self):
        with self.client() as client:
            auth = self.auth(client)
            item_id = self.seed_ledger(client, auth)
            movement_id, = self.snapshot("SELECT MIN(id) FROM inventory_movements")[0]
            older_page = f"/inventory/{item_id}?before=3"
            route = f"/inventory/{item_id}/movements/{movement_id}/reverse"
            draft = form_for(client.get(older_page), route)
            draft["reason"] = "Correct duplicate delivery"
            self.post(client, f"{INVENTORY}/{item_id}/receive", auth, {"revision": 28, "quantity_milli": 2000})
            audits = self.snapshot("SELECT id FROM audit_events")
            receipts = self.snapshot("SELECT COUNT(*) FROM stock_requests")
            stale = client.post(route, data=draft)
            self.assertEqual(stale.status_code, 409)
            self.assertIn(f"Review reversal of movement #{movement_id}", stale.text)
            self.assertIn('value="Correct duplicate delivery"', stale.text)
            self.assertIn("29 pcs", stale.text)
            self.assertIn("Original receipt", stale.text)
            review = form_for(stale, route, reviewed=True)
            self.assertEqual(review["before"], "3")
            self.assertEqual(review["revision"], "29")
            self.assertNotEqual(review["key"], draft["key"])
            self.assertEqual(self.snapshot("SELECT id FROM audit_events"), audits)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM stock_requests"), receipts)
            self.assertEqual(self.snapshot("SELECT on_hand_milli FROM inventory_items"), [(29000,)])
            response = client.post(route, data=review | {"reason": draft["reason"]})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], older_page + "&saved=1")
            self.assertEqual(client.post(route, data=review | {"reason": draft["reason"]}).status_code, 303)
            self.assertEqual(self.snapshot("SELECT reverses_id, quantity_milli, reason FROM inventory_movements "
                                           "WHERE kind='reversal'"), [(movement_id, -1000, draft["reason"])])
            self.assertEqual(self.snapshot("SELECT on_hand_milli FROM inventory_items"), [(28000,)])
            already = client.post(route, data=draft | {"key": uuid4().hex})
            self.assertEqual(already.status_code, 409)
            self.assertNotIn(f'action="{route}"', already.text)

    def test_stale_equal_stage_acknowledges_match_without_another_write(self):
        with self.client() as first, self.client() as second:
            auth = self.auth(first)
            self.sign_in(second)
            repair = self.post(first, API, auth, DEVICE)
            url = f"/repairs/{repair['id']}"
            action = url + "/stage"
            draft = form_for(second.get(url), action) | {"stage": "in_progress"}
            self.post(first, f"{API}/{repair['id']}/stage", auth,
                      {"revision": 1, "stage": "in_progress"}, status=200)
            request_count = self.snapshot("SELECT COUNT(*) FROM repair_requests")
            response = second.post(action, data=draft)
            self.assertEqual(response.status_code, 409)
            self.assertIn("The repair already has that status.", response.text)
            self.assertNotIn('id="stage-review-form"', response.text)
            refreshed = form_for(response, action)
            self.assertEqual(refreshed["revision"], "2")
            self.assertNotEqual(refreshed["key"], draft["key"])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM repair_requests"), request_count)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='repair.stage'"), [(1,)])
            self.assertEqual(self.snapshot("SELECT stage, revision FROM repairs"), [("in_progress", 2)])
