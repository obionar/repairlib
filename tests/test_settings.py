"""S1c shop settings, tenant boundaries and configuration ownership gates."""

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

SETTINGS = "/settings"
API = "/api/v1/settings"


class SettingsTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def settings_form(self, client, **overrides):
        page = client.get(SETTINGS)
        self.assertEqual(page.status_code, 200)
        return {
            "csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
            "revision": hidden(page, "revision"), "name": hidden(page, "name"),
            "timezone": hidden(page, "timezone"), **overrides,
        }

    def test_browser_api_retry_restart_and_validation(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.assertEqual(client.get(API, headers=auth).json(), {
                "name": "Synthetic", "timezone": "UTC", "revision": 1, "max_upload_mib": 256, "inline_preview_mib": 8, "language": "",
                "address": "", "phone": "", "terms": "",
            })
            page = client.get(SETTINGS)
            self.assertNotIn("Changes to these shop settings apply immediately.", page.text)
            self.assertNotIn("No restart is pending.", page.text)
            self.assertIn("listener and database configuration", page.text)
            form = self.settings_form(client, name="Εργαστήριο επισκευών", timezone="Europe/Nicosia")
            self.assertEqual(client.post(SETTINGS, data=form).status_code, 303)
            self.assertEqual(client.post(SETTINGS, data=form).status_code, 303)
            expected = {"name": form["name"], "timezone": form["timezone"], "revision": 2,
                        "max_upload_mib": 256, "inline_preview_mib": 8, "language": "",
                        "address": "", "phone": "", "terms": ""}
            self.assertEqual(client.get(API, headers=auth).json(), expected)
            replay = client.patch(API, headers={**auth, "Idempotency-Key": form["key"]}, json={
                "revision": 1, "name": form["name"], "timezone": form["timezone"],
            })
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), expected)
            mismatch = client.patch(API, headers={**auth, "Idempotency-Key": form["key"]},
                                    json={"revision": 1, "name": "Different"})
            self.assertEqual(mismatch.status_code, 409)
            invalid = self.settings_form(client, timezone="Not/A_Real_Zone")
            rejected = client.post(SETTINGS, data=invalid)
            self.assertEqual(rejected.status_code, 422)
            self.assertIn('value="Not/A_Real_Zone"', rejected.text)
            self.assertIn('value="Εργαστήριο επισκευών"', rejected.text)
            stale = client.patch(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                 json={"revision": 1, "timezone": "UTC"})
            self.assertEqual(stale.status_code, 409)
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.assertEqual(client.get(API, headers=auth).json(), expected)
        self.assertEqual(self.snapshot(
            "SELECT operation,source,entity_type,entity_id,changes FROM audit_events "
            "WHERE operation='settings.update'"
        ), [("settings.update", "web", "shop_settings", 1,
             json.dumps({"name": {"before": "Synthetic", "after": form["name"]},
                         "timezone": {"before": "UTC", "after": "Europe/Nicosia"}}))])

    def test_non_admin_and_second_shop_settings_are_refused_without_side_effects(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            for username, role in (("technician", "technician"), ("viewer", "viewer")):
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,?,?,password_hash FROM users WHERE id=1", (username, role))
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
            db.execute("INSERT INTO shop_settings(shop_id,timezone) VALUES (2,'Asia/Tokyo')")
        with self.client() as owner, self.client() as other:
            _, auth = self.admin_auth(owner)
            for username in ("technician", "viewer"):
                self.sign_in(other, username)
                self.assertEqual(other.get(SETTINGS).status_code, 403)
                self.assertEqual(other.post(SETTINGS, data={
                    "csrf": "invalid", "key": uuid4().hex, "revision": "1",
                    "name": "Denied", "timezone": "UTC",
                }).status_code, 403)
                client = other
                token = issued_secret(client.post(ACCOUNT, data={
                    "csrf": hidden(client.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                    "name": f"{username} read", "scope": "read", "password": PASSWORD,
                }))
                self.assertEqual(other.get(API, headers={"Authorization": f"Bearer {token}"}).status_code, 403)
                other.post("/logout", data={"csrf": hidden(other.get("/account"), "csrf")})
            changed = owner.patch(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 1, "name": "Local only"})
            self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.snapshot("SELECT name FROM shops WHERE id=2"), [("Outside",)])
        self.assertEqual(self.snapshot("SELECT timezone,revision FROM shop_settings WHERE shop_id=2"),
                         [("Asia/Tokyo", 1)])
        self.assertEqual(self.snapshot(
            "SELECT COUNT(*) FROM audit_events WHERE shop_id=2 AND operation='settings.update'"
        ), [(0,)])

    def test_real_http_cli_settings_show_edit_and_retry(self):
        with workspace_client(self.workspace) as (client, _):
            token, _ = self.admin_auth(client)

            def cli(*args, code=0):
                result = subprocess.run(
                    [*COMMAND, "settings", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None

            self.assertEqual(cli("show"), {"name": "Synthetic", "timezone": "UTC", "revision": 1,
                                           "max_upload_mib": 256, "inline_preview_mib": 8, "language": "",
                                           "address": "", "phone": "", "terms": ""})
            key = uuid4().hex
            edited = cli("edit", "--revision", "1", "--key", key, "--timezone", "Europe/Athens")
            self.assertEqual(edited, {"name": "Synthetic", "timezone": "Europe/Athens", "revision": 2,
                                      "max_upload_mib": 256, "inline_preview_mib": 8, "language": "",
                                      "address": "", "phone": "", "terms": ""})
            self.assertEqual(cli("edit", "--revision", "1", "--key", key,
                                 "--timezone", "Europe/Athens"), edited)
            cli("edit", "--revision", "1", "--key", uuid4().hex, "--name", "Stale", code=7)
            self.assertEqual(cli("show"), edited)

    def test_cli_sets_and_shows_the_handover_contact_block_terms_and_language(self):
        with workspace_client(self.workspace) as (client, _):
            self.admin_auth(client)

            def cli(*args, json_output=True):
                result = subprocess.run(
                    [*COMMAND, "settings", *args, "--workspace", str(self.workspace),
                     *(["--json"] if json_output else [])],
                    text=True, capture_output=True, timeout=10, env=python_env(),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            edited = json.loads(cli("edit", "--revision", "1", "--key", uuid4().hex,
                                    "--language", "el", "--address", "1 Handover Street",
                                    "--phone", "+357 22 000000", "--terms", "Collect within 30 days."))
            self.assertEqual((edited["language"], edited["address"], edited["phone"], edited["terms"]),
                             ("el", "1 Handover Street", "+357 22 000000", "Collect within 30 days."))
            self.assertEqual(json.loads(cli("show")), edited)
            plain = cli("show", json_output=False)
            for line in ("Language\tel", "Address\t1 Handover Street", "Phone\t+357 22 000000",
                         "Terms\tCollect within 30 days."):
                self.assertIn(line, plain)
            cleared = json.loads(cli("edit", "--revision", "2", "--key", uuid4().hex, "--address", ""))
            self.assertEqual((cleared["address"], cleared["phone"]), ("", "+357 22 000000"))

