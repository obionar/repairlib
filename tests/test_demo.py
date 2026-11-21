"""Synthetic demo initialization must be explicit, isolated and restartable."""

from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.domain.sessions import login
from test_baseline import python_env
from test_cli_identity import COMMAND

PASSWORD = "synthetic demo owner passphrase"


class DemoTests(unittest.TestCase):
    def test_workshop_fixture_populates_ticket_device_details(self):
        dataset = Path(__file__).resolve().parents[1] / "scripts" / "demo-seed.json"
        repairs = [repair for customer in json.loads(dataset.read_text()) for repair in customer["repairs"]]
        self.assertTrue(repairs)
        for repair in repairs:
            with self.subTest(device=repair["device_type"]):
                for key in ("device_type", "brand", "model", "serial", "display_device", "reported_issue", "notes"):
                    self.assertTrue(repair.get(key), key)
        self.assertEqual(len({repair["serial"] for repair in repairs}), len(repairs))

    def test_presentation_seed_stocks_a_searchable_catalog(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            setup = subprocess.run([*COMMAND, "setup", "--workspace", str(workspace), "--shop", "Demo Shop",
                                    "--timezone", "Europe/Nicosia", "--username", "owner"],
                                   input=PASSWORD + "\n", text=True, capture_output=True, timeout=30, env=python_env())
            self.assertEqual(setup.returncode, 0, setup.stderr)
            seeded = subprocess.run(["python3", str(root / "scripts" / "seed_demo.py"), "--workspace", str(workspace)],
                                    text=True, capture_output=True, timeout=60, env=python_env())
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
                stocked = db.execute("SELECT count(*) FROM inventory_items WHERE on_hand_milli > 0 "
                                     "AND selling_price_cents > 0").fetchone()[0]
                self.assertGreaterEqual(stocked, 3)
                self.assertTrue(db.execute("SELECT 1 FROM inventory_items WHERE description LIKE '%port%'").fetchone())

    def test_demo_command_seeds_only_a_new_workspace_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace with spaces"
            command = [*COMMAND, "demo", "--workspace", str(workspace), "--username", "owner"]
            created = subprocess.run(command, input=PASSWORD + "\n", text=True,
                                     capture_output=True, timeout=15, env=python_env())
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertNotIn(PASSWORD, created.stdout + created.stderr)
            database = workspace / "state" / "repairlib.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT name FROM shops").fetchall(),
                                 [("Synthetic Repair Shop",)])
                self.assertEqual(db.execute("SELECT role,active FROM users").fetchall(),
                                 [("admin", 1)])
                customers = db.execute("SELECT name,email,notes FROM customers ORDER BY id").fetchall()
                self.assertEqual(len(customers), 3)
                self.assertTrue(all("synthetic" in notes.lower() for _, _, notes in customers))
                audits = db.execute(
                    "SELECT actor_kind,actor_user_id,operation,source,changes FROM audit_events ORDER BY id"
                ).fetchall()
                self.assertEqual([(row[0], row[1], row[2], row[3]) for row in audits],
                                 [("maintenance", None, "setup", None),
                                  ("maintenance", None, "demo.seed", "cli")])
                self.assertNotIn(PASSWORD, repr(audits))
                before = db.execute("SELECT * FROM customers ORDER BY id").fetchall()

            self.assertIsNotNone(login(workspace, "owner", PASSWORD, now=2_000_000_000))
            with TestClient(create_app(workspace=workspace), base_url="http://127.0.0.1",
                            follow_redirects=False) as client:
                page = client.get("/login")
                from test_tokens import hidden
                signed_in = client.post("/login", data={
                    "username": "owner", "password": PASSWORD, "csrf": hidden(page, "csrf"),
                })
                self.assertEqual(signed_in.status_code, 303)
                listing = client.get("/customers")
                self.assertEqual(listing.status_code, 200)
                for name, _, _ in customers:
                    self.assertIn(name, listing.text)

            refused = subprocess.run(command, input="different passphrase\n", text=True,
                                     capture_output=True, timeout=15, env=python_env())
            self.assertNotEqual(refused.returncode, 0)
            self.assertNotIn("different passphrase", refused.stdout + refused.stderr)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT * FROM customers ORDER BY id").fetchall(), before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone(), (1,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone(), (4,))


if __name__ == "__main__":
    unittest.main()
