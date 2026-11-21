"""Admin system page and a support bundle without customer data."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from zipfile import ZipFile

from repairlib.domain import system as system_domain
from repairlib.storage.initialization import SCHEMA_VERSION
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_tokens import TokenFixture

SYSTEM = "/system"


class AdminStatusTests(TokenFixture, unittest.TestCase):
    def add_reader(self, role="viewer"):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""INSERT INTO users(id, shop_id, username, role, password_hash)
                          SELECT 2, 1, ?, ?, password_hash FROM users WHERE id = 1""", ("reader", role))

    def customer_fact(self):
        """Customer data that must never reach the page or the bundle."""
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""INSERT INTO customers(id, shop_id, name, phone)
                          VALUES (7, 1, 'Eleni Secret', '+35799123456')""")
        return "Eleni Secret", "+35799123456"

    def test_page_requires_sign_in_and_admin(self):
        self.add_reader()
        with self.client() as client:
            self.assertEqual(client.get(SYSTEM).status_code, 303)
            self.sign_in(client, "reader")
            self.assertEqual(client.get(SYSTEM).status_code, 403)
        with self.client() as client:
            self.sign_in(client)
            self.assertEqual(client.get(SYSTEM).status_code, 200)

    def test_page_reports_system_facts_and_no_customer_data(self):
        name, phone = self.customer_fact()
        with self.client() as client:
            self.sign_in(client)
            page = client.get(SYSTEM).text
            for label in ("Schema", "Workspace", "Last verified backup", "Printer", "Sessions"):
                self.assertIn(label, page)
            self.assertIn(str(self.workspace), page)
            self.assertNotIn(name, page)
            self.assertNotIn(phone, page)

    def bundle(self, output):
        return subprocess.run([*COMMAND, "support", "bundle", "--workspace", str(self.workspace),
                               "--output", str(output)], capture_output=True, text=True,
                              env=python_env(), timeout=60)

    def test_support_bundle_carries_facts_and_no_customer_data(self):
        name, phone = self.customer_fact()
        output = self.workspace.parent / "support.zip"
        result = self.bundle(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {"status.json", "system.txt"})
            blob = b"".join(archive.read(entry) for entry in names).decode()
            status = json.loads(archive.read("status.json"))
        self.assertEqual(status["schema_version"], SCHEMA_VERSION)
        self.assertIn("Sessions", blob)
        self.assertNotIn(name, blob)
        self.assertNotIn(phone, blob)
        self.assertEqual(self.bundle(output).returncode, 1)

    def test_clock_skew_is_reported_against_the_newest_record(self):
        base = system_domain.facts(self.workspace)
        self.assertFalse(base["clock_warning"])
        skewed = system_domain.facts(self.workspace, now=base["now"] - 3600)
        self.assertTrue(skewed["clock_warning"])
        self.assertIn("clock", " ".join(system_domain.lines(skewed)).lower())


if __name__ == "__main__":
    unittest.main()
