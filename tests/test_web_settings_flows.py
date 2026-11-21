"""UI-5 handover and role-scoped settings through native browser routes."""

from contextlib import closing
from datetime import datetime
import sqlite3
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from test_charges import PART
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_feedback import form_for
from test_web_repair_workspace import panel


class WebSettingsFlowsTests(TokenFixture, unittest.TestCase):
    def test_default_diagnosis_fee_and_saved_ticket_amounts(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}

            def create(path, data):
                result = client.post(path, json=data, headers=auth | {"Idempotency-Key": uuid4().hex})
                self.assertEqual(result.status_code, 201)
                return result.json()

            def change_default(amount):
                fields = form_for(client.get("/settings"), "/settings")
                response = client.post("/settings", data=fields | {"diagnosis_fee": amount})
                self.assertEqual(response.status_code, 303)

            repair = create(API, DEVICE)
            api, ticket = f"{API}/{repair['id']}", f"/repairs/{repair['id']}"
            action = ticket + "/fees"
            self.assertRegex(panel(client.get(ticket), "payment-total"), r'name="diagnosis"[^>]*value="20\.00"')
            change_default("25,00")
            self.assertRegex(panel(client.get(ticket), "payment-total"), r'name="diagnosis"[^>]*value="25\.00"')
            part = create(api + "/charges", PART)
            page = client.get(ticket)
            fields = form_for(page, action) | {"diagnosis": "20.00", "labor": "35.50", "discount": "5"}
            self.assertEqual(client.post(action, data=fields | {"labor": "-1"}).status_code, 422)
            stale = form_for(page, action) | {"labor": "1"}
            self.assertEqual(client.post(action, data=fields).status_code, 303)
            self.assertEqual(client.post(action, data=fields).status_code, 303)
            self.assertEqual(client.post(action, data=stale).status_code, 409)
            change_default("30.00")
            summary = client.get(api + "/charges", headers=auth).json()
            self.assertEqual(summary["parts_subtotal_cents"], part["line_total_cents"])
            self.assertEqual(summary["total_cents"], 5050 + part["line_total_cents"])
            self.assertEqual(len(summary["items"]), 3)
            saved_form = panel(client.get(ticket), "payment-total")
            # A saved diagnosis keeps its own amount even after the shop default changes.
            self.assertRegex(saved_form, r'name="diagnosis"[^>]*value="20\.00"')
            self.assertRegex(saved_form, r'name="labor"[^>]*value="35\.50"')

    def test_settings_groups_and_account_keep_roles_separate(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                       "SELECT shop_id, 'reader', 'viewer', password_hash FROM users WHERE id=1")
        with self.client() as admin, self.client() as reader:
            self.sign_in(admin)
            self.sign_in(reader, "reader")
            settings = admin.get("/settings")
            for group in ("shop-profile", "file-limits"):
                self.assertIn(f'id="{group}"', settings.text)
            for path in ("/settings", "/settings/printer", "/users"):
                self.assertIn(f'href="{path}"', panel(settings, "settings-navigation"))
                self.assertEqual(reader.get(path).status_code, 403)
            account = reader.get("/account")
            profile = panel(account, "account-profile")
            for label in ("reader", "Viewer", "Synthetic"):
                self.assertIn(label, profile)
            self.assertIn('id="language-choice"', panel(account, "account-language"))
            self.assertIn('href="/account/tokens"', panel(account, "account-api"))
            self.assertNotIn('href="/settings"', account.text)
            self.assertNotIn('href="/users"', account.text)

    def test_ticket_dates_pickup_queue_and_print_use_shop_time(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET timezone='Asia/Tokyo' WHERE shop_id=1")
            db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                       "SELECT shop_id, 'reader', 'viewer', password_hash FROM users WHERE id=1")

        def write(client, path, expected, **fields):
            response = client.post(path, json=fields, headers=auth | {"Idempotency-Key": uuid4().hex})
            self.assertEqual(response.status_code, expected, response.text)
            return response.json()

        def local(value, lang="en"):
            pattern = "%d/%m/%Y %H:%M" if lang == "el" else "%Y-%m-%d %H:%M"
            return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Tokyo")).strftime(pattern)

        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            record = write(client, API, 201, **DEVICE)
            path = f"{API}/{record['id']}"
            detail = f"/repairs/{record['id']}"
            write(client, path + "/charges", 201, **PART)
            for lang in ("en", "el"):
                page = client.get(detail, headers={"Accept-Language": lang})
                utc = record["created_at"]
                self.assertIn(f'<time datetime="{utc}">{local(utc, lang)}</time>', page.text)
            write(client, path + "/stage", 200, revision=1, stage="in_progress")
            write(client, path + "/stage", 200, revision=2, stage="repaired")
            queue = "/repairs?queue=pickup"
            self.assertIn(f'href="{detail}"', client.get(queue).text)
            status_url = detail + "/stage"
            reply = client.post(status_url, data={**form_for(client.get(detail), status_url), "pickup": "1"})
            self.assertEqual(reply.status_code, 303)
            self.assertNotIn(f'href="{detail}"', client.get(queue).text)

        with self.client() as restarted:
            self.sign_in(restarted, "reader")
            record = restarted.get(path, headers=auth).json()
            printed = restarted.get(detail + "/print")
            self.assertEqual(printed.status_code, 200)
            self.assertIn("Picked up", printed.text)
            self.assertIn(local(record["picked_up_at"]), printed.text)
            self.assertNotIn(f'href="{detail}"', restarted.get(queue).text)
