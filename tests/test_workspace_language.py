"""Workspace default language: storage, setup, settings control and negotiation."""

from contextlib import closing
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from repairlib import i18n
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_settings import SETTINGS
from test_tokens import PASSWORD, TokenFixture
from test_users import hidden

LOGIN = "/login"


class WorkspaceLanguageTests(TokenFixture, unittest.TestCase):
    def store(self, value):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET language = ? WHERE shop_id = 1", (value,))

    def stored(self):
        return self.snapshot("SELECT language, revision FROM shop_settings WHERE shop_id = 1")

    def page_language(self, client, **headers):
        page = client.get(LOGIN, headers=headers)
        self.assertEqual(page.status_code, 200)
        return page.text.split('<html lang="', 1)[1].split('"', 1)[0]

    def test_negotiation_order_is_cookie_header_stored_default(self):
        self.assertEqual(i18n.negotiate("", ""), "en")
        self.assertEqual(i18n.negotiate("", "", "el"), "el")
        self.assertEqual(i18n.negotiate("", "fr", "el"), "el")
        self.assertEqual(i18n.negotiate("", "en", "el"), "en")
        self.assertEqual(i18n.negotiate("en", "el", "el"), "en")
        self.assertEqual(i18n.negotiate("", "", "xx"), "en")

    def test_stored_default_applies_when_request_carries_no_preference(self):
        self.store("el")
        with self.client() as client:
            self.assertEqual(self.page_language(client), "el")
            self.assertEqual(self.page_language(client, **{"Accept-Language": "fr"}), "el")
            self.assertIn("Σύνδεση", client.get(LOGIN).text)

    def test_header_and_cookie_still_beat_the_stored_default(self):
        self.store("el")
        with self.client() as client:
            self.assertEqual(self.page_language(client, **{"Accept-Language": "en"}), "en")
            client.cookies.set(i18n.COOKIE, "en")
            self.assertEqual(self.page_language(client), "en")
            self.assertEqual(self.page_language(client, **{"Accept-Language": "el"}), "en")

    def test_unset_or_unknown_stored_value_keeps_english(self):
        with self.client() as client:
            self.assertEqual(self.stored(), [("", 1)])
            self.assertEqual(self.page_language(client), "en")
            self.store("xx")
            self.assertEqual(self.page_language(client), "en")

    def test_settings_control_stores_default_and_refuses_stale_or_unknown(self):
        with self.client() as client:
            self.sign_in(client)
            form = self.settings_form(client)
            page = client.get(SETTINGS).text
            self.assertIn('name="language"', page)
            self.assertEqual(client.post(SETTINGS, data={**form, "language": "xx"}).status_code, 422)
            self.assertEqual(self.stored(), [("", 1)])
            self.assertEqual(client.post(SETTINGS, data={**form, "language": "el"}).status_code, 303)
            self.assertEqual(self.stored(), [("el", 2)])
            stale = {**form, "key": uuid4().hex, "language": "en"}
            self.assertEqual(client.post(SETTINGS, data=stale).status_code, 409)
            self.assertEqual(self.stored(), [("el", 2)])
            changes = self.snapshot("SELECT changes FROM audit_events WHERE operation='settings.update'")
            self.assertEqual(len(changes), 1)
            self.assertIn('"language": {"before": "", "after": "el"}', changes[0][0])
        with self.client() as fresh:
            self.assertEqual(self.page_language(fresh), "el")

    def settings_form(self, client):
        page = client.get(SETTINGS)
        self.assertEqual(page.status_code, 200)
        return {name: hidden(page, name) for name in ("csrf", "key", "revision", "name", "timezone")}


class SetupLanguageTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "workspace"
        self.db = self.workspace / "state" / "repairlib.sqlite3"

    def setup_cli(self, language):
        return subprocess.run(
            [*COMMAND, "setup", "--workspace", str(self.workspace), "--shop", "Synthetic Shop",
             "--timezone", "Europe/Nicosia", "--username", "owner", "--language", language],
            input=PASSWORD + "\n", capture_output=True, text=True, env=python_env(), timeout=30,
        )

    def test_setup_stores_a_known_language(self):
        result = self.setup_cli("el")
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT language FROM shop_settings").fetchall(), [("el",)])

    def test_setup_refuses_an_unknown_language_and_stores_nothing(self):
        result = self.setup_cli("xx")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("xx", result.stderr)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
