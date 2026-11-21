"""Visible switch user and the session-only cookie choice."""

from contextlib import closing
import hashlib
import re
import sqlite3
import time
import unittest

from fastapi.testclient import TestClient

from repairlib.app import create_app
from test_tokens import ORIGIN, PASSWORD, TokenFixture, hidden

SESSION_COOKIE = "repairlib_session"
NAV = re.compile(r'<nav [^>]*id="primary-navigation"[^>]*>.*?</nav>', re.S)
LOGIN_FORM = re.compile(r'<form method="post" action="/logout" id="nav-logout">(.*?)</form>', re.S)


def navigation(response):
    match = NAV.search(response.text)
    if not match:
        raise AssertionError("Missing primary navigation")
    return match[0]


def switch_csrf(response):
    match = re.search(r'<input [^>]*form="nav-logout" name="csrf" value="([^"]+)"', response.text)
    if not match:
        raise AssertionError("Missing navigation sign-out token")
    return match[1]


def session_cookie(response):
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(SESSION_COOKIE + "="):
            return header
    raise AssertionError("Response set no session cookie")


def expires_at(database, header):
    token = header.split(";", 1)[0].split("=", 1)[1]
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (digest,)).fetchall()
    if len(rows) != 1:
        raise AssertionError(f"Expected one stored session, found {rows!r}")
    return rows[0][0]


class SessionSwitchTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                       "SELECT 1, 'tech', 'technician', password_hash FROM users WHERE username = 'owner'")

    def login(self, client, **extra):
        page = client.get("/login")
        return client.post("/login", data={
            "username": "owner", "password": PASSWORD, "csrf": hidden(page, "csrf"), **extra,
        })

    def test_nav_switch_user_reuses_the_csrf_sign_out_form(self):
        with self.client() as client:
            self.sign_in(client)
            form = LOGIN_FORM.search(navigation(client.get("/repairs")))
            self.assertIsNotNone(form, "sign-out form missing from navigation")
            body = form[1] if form else ""
            self.assertIn('name="switch" value="1"', body)
            self.assertIn("Switch user", body)
            self.assertIn("Sign out", body)
            before = self.snapshot("SELECT COUNT(*), max(rowid) FROM sessions")
            for data in ({"csrf": "invalid", "switch": "1"}, {"csrf": "invalid"}):
                self.assertEqual(client.post("/logout", data=data).status_code, 403)
            self.assertEqual(self.snapshot("SELECT COUNT(*), max(rowid) FROM sessions"), before)
            self.assertEqual(client.get("/repairs").status_code, 200)
            greek = client.get("/repairs", cookies={"repairlib_lang": "el"})
            self.assertIn("Αλλαγή χρήστη", greek.text)
            self.assertIn("Αποσύνδεση", greek.text)
        with self.client() as guest:
            self.assertIn("Σύνδεση μόνο για αυτή τη συνεδρία περιήγησης",
                          guest.get("/login", cookies={"repairlib_lang": "el"}).text)

    def test_switch_user_signs_out_and_the_next_user_can_login(self):
        with self.client() as client:
            self.sign_in(client)
            csrf = switch_csrf(client.get("/repairs"))
            switched = client.post("/logout", data={"csrf": csrf, "switch": "1"})
            self.assertEqual((switched.status_code, switched.headers["location"]), (303, "/login?switch=1"))
            self.assertIn("max-age=0", session_cookie(switched).lower())
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM sessions"), [(0,)])
            self.assertEqual(self.snapshot("SELECT operation FROM audit_events ORDER BY id")[-2:],
                             [("login",), ("logout",)])
            self.assertEqual(client.get("/").headers["location"], "/login")
            page = client.get(switched.headers["location"])
            self.assertIn("Signed out. Choose the user for this device.", page.text)
            self.assertIn('id="session-only"', page.text)
            reply = client.post("/login", data={
                "username": "tech", "password": PASSWORD, "csrf": hidden(page, "csrf"),
            })
            self.assertEqual((reply.status_code, reply.headers["location"]), (303, "/"))
            account = client.get("/account")
            self.assertIn("tech", account.text)
            self.assertIn("Technician", account.text)
            self.assertEqual(self.snapshot("SELECT operation FROM audit_events ORDER BY id")[-1:],
                             [("login",)])
            self.assertEqual(self.snapshot(
                "SELECT username FROM audit_events JOIN users ON users.id = audit_events.actor_user_id "
                "ORDER BY audit_events.id DESC LIMIT 1"), [("tech",)])

    def test_session_only_cookie_omits_max_age_and_keeps_flags_and_expiry(self):
        with self.client() as client:
            default = self.login(client)
            self.assertEqual(default.status_code, 303)
            header = session_cookie(default)
            lowered = header.lower()
            for flag in ("path=/", "httponly", "samesite=strict", "max-age=43200"):
                self.assertIn(flag, lowered)
            self.assertNotIn("secure", lowered)  # loopback HTTP keeps the flag off
            self.assertLess(abs(expires_at(self.db, header) - (int(time.time()) + 43200)), 5)
        with self.client() as client:
            session_only = self.login(client, session_only="1")
            self.assertEqual(session_only.status_code, 303)
            header = session_cookie(session_only)
            lowered = header.lower()
            self.assertNotIn("max-age", lowered)
            self.assertNotIn("expires", lowered)
            for flag in ("path=/", "httponly", "samesite=strict"):
                self.assertIn(flag, lowered)
            self.assertLess(abs(expires_at(self.db, header) - (int(time.time()) + 43200)), 5)
        https = TestClient(create_app(workspace=self.workspace), base_url="https://127.0.0.1",
                           follow_redirects=False)
        with https as client:
            page = client.get("/login")
            self.assertEqual(page.status_code, 200)
            reply = client.post("/login", data={
                "username": "owner", "password": PASSWORD, "csrf": hidden(page, "csrf"),
                "session_only": "1",
            })
            self.assertEqual(reply.status_code, 303)
            header = session_cookie(reply)
            self.assertIn("secure", header.lower())
            self.assertNotIn("max-age", header.lower())

    def test_session_only_choice_survives_a_refused_login(self):
        with self.client() as client:
            page = client.get("/login")
            refused = client.post("/login", data={
                "username": "owner", "password": "wrong", "csrf": hidden(page, "csrf"),
                "session_only": "1",
            })
            self.assertEqual(refused.status_code, 401)
            self.assertRegex(refused.text, r'<input id="session-only"[^>]* checked>')
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM sessions"), [(0,)])


if __name__ == "__main__":
    unittest.main()
