"""S3 dashboard parity through authenticated browser routes."""

from contextlib import closing
import re
import sqlite3
import unittest

from test_tokens import PASSWORD, TokenFixture, hidden


def seed_dashboard(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
        db.executemany(
            """INSERT INTO users(id, shop_id, username, role, password_hash)
               SELECT ?, ?, ?, ?, password_hash FROM users WHERE username = 'owner'""",
            [(2, 1, "tech", "technician"), (3, 1, "reader", "viewer"), (4, 2, "outsider", "admin")],
        )
        db.executemany(
            "INSERT INTO customers(id, shop_id, name, archived_at) VALUES (?, ?, ?, ?)",
            [(i, 1, f"Synthetic customer {i}", None) for i in range(1, 8)]
            + [(8, 1, "Archived contact", "2026-01-02"), (9, 2, "Outside contact", None)],
        )
        db.executemany(
            """INSERT INTO repairs(id, shop_id, customer_id, technician_id, stage, created_at,
                                   picked_up_at, archived_at, reported_issue)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (1, 1, 7, 2, "new", "2026-01-01", None, None, "Intake"),
                (2, 1, None, None, "waiting_parts", "2026-01-01", None, None, "Part order"),
                (3, 1, None, None, "lab_test", "2026-01-01", None, None, "Bench test"),
                (4, 1, None, None, "customer_test", "2026-01-01", None, None, "Customer test"),
                (5, 1, None, None, "in_progress", "2026-01-03", None, None, "Inspection"),
                (6, 1, None, 2, "in_progress", "2026-01-01", "2026-01-05", None, "Early pickup"),
                (7, 1, None, None, "in_progress", "2026-01-01", None, None, "Same intake date"),
                (8, 1, None, None, "new", "2026-01-03", None, None, "Second intake"),
                (9, 1, None, None, "new", "2026-01-04", None, None, "Third intake"),
                (10, 1, None, None, "new", "2026-01-01", None, "2026-01-02", "Archived open"),
                (11, 1, 7, None, "repaired", "2026-01-01", None, None, "Screen fixed"),
                (12, 1, 8, None, "no_repair", "2026-01-02", None, None, "Declined repair"),
                (13, 1, None, None, "no_repair", "2026-01-01", "2026-01-02", None, "Collected"),
                (14, 1, None, None, "repaired", "2026-01-01", None, "2026-01-02", "Archived outcome"),
                (99, 2, 9, 4, "in_progress", "2025-01-01", None, None, "Outside open"),
                (100, 2, 9, 4, "no_repair", "2025-01-01", None, None, "Outside outcome"),
            ] + [(i, 1, None, None, "repaired", "2026-01-07", None, None, "Ready")
                 for i in range(15, 22)],
        )
        db.executemany(
            """INSERT INTO audit_events(shop_id, actor_kind, operation, occurred_at, entity_type, entity_id)
               VALUES (?, 'maintenance', ?, ?, ?, ?)""",
            [
                (1, "repair.stage", "2026-01-04", "repair", 11),
                (1, "repair.update", "2026-01-06", "repair", 11),
                (1, "repair.stage", "2026-01-05", "repair", 12),
                (1, "work_entry.create", "2026-01-08", "work_entry", 12),
                (2, "repair.stage", "2026-01-09", "repair", 12),
            ],
        )


def queue(html, name):
    match = re.search(rf'<section [^>]*aria-labelledby="{name}"[^>]*>(.*?)</section>', html, re.S)
    if not match:
        raise AssertionError(f"Missing dashboard section {name}")
    return match[1]


def links(html, route):
    return [int(value) for value in re.findall(rf'href="/{route}/(\d+)"', html)]


class DashboardTests(TokenFixture, unittest.TestCase):
    def test_empty_dashboard_and_navigation(self):
        with self.client() as client:
            self.assertEqual(client.get("/").headers.get("location"), "/login")
            self.assertEqual(client.get("/account").status_code, 303)
            self.sign_in(client)
            dashboard = client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn('<header class="rl-page-header rl-sr-only">', dashboard.text)
            self.assertNotIn('>New repair</a>', dashboard.text)
            self.assertNotIn('>New customer</a>', dashboard.text)
            self.assertIn("No open work.", queue(dashboard.text, "open-work"))
            self.assertIn("No repairs awaiting pickup.", queue(dashboard.text, "awaiting-pickup"))
            self.assertIn("No customers on this page.", queue(dashboard.text, "recent-customers"))
            for url, label in (("/repairs", "Repairs"), ("/customers", "Customers"), ("/account", "Account")):
                with self.subTest(url=url):
                    self.assertIn(f'href="{url}"', dashboard.text)
                    destination = client.get(url)
                    self.assertEqual(destination.status_code, 200)
                    self.assertIn(f">{label}</h1>", destination.text)
                    self.assertIn('href="/"', destination.text)
            self.assertIn('href="/account/tokens"', client.get("/account").text)

    def test_bounded_queues_show_shop_work_to_all_operational_roles(self):
        seed_dashboard(self.db)
        for username in ("owner", "tech", "reader"):
            with self.subTest(username=username), self.client() as client:
                self.sign_in(client, username)
                result = client.get("/?shop_id=2&technician_id=4")
                self.assertEqual(result.status_code, 200)
                work = queue(result.text, "open-work")
                pickup = queue(result.text, "awaiting-pickup")
                customers = queue(result.text, "recent-customers")
                self.assertIn("9 open", work)
                self.assertIn("9 waiting", pickup)
                self.assertEqual(links(work, "repairs"), [6, 7, 5, 3, 4, 1, 8, 9])
                self.assertEqual(links(pickup, "repairs"), [12, 11, 15, 16, 17, 18, 19, 20])
                self.assertEqual(links(customers, "customers"), [7, 6, 5, 4, 3])
                self.assertIn("Synthetic customer 7", work)
                self.assertEqual(links(pickup, "customers"), [8, 7])
                self.assertNotIn("Outside", result.text)
                for path in ("/repairs/6", "/repairs/12", "/customers/8"):
                    self.assertEqual(client.get(path).status_code, 200)
                for path in ("/repairs/99", "/repairs/100", "/customers/9"):
                    self.assertEqual(client.get(path).status_code, 404)

        with self.client() as client:
            login = client.get("/login")
            self.assertEqual(client.post("/login", data={
                "username": "outsider", "password": PASSWORD, "csrf": hidden(login, "csrf"),
            }).status_code, 401)
            self.assertEqual(client.get("/").headers.get("location"), "/login")
