"""HTTP contract gates for browser setup and authentication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env


ORIGIN = "http://127.0.0.1"
FIELDS = {"shop": "Synthetic <Shop>", "timezone": "UTC", "username": "owner"}
PASSWORD = "synthetic & password + δοκιμή"


def csrf(page):
    match = re.search(r'name="csrf" value="([A-Za-z0-9_-]+)"', page.text)
    return match[1] if match else None


class BrowserAuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "browser"
        self.database = self.workspace / "state" / "repairlib.sqlite3"
        self.bootstrap = secrets.token_urlsafe(32)

    def client(self, *, setup=False):
        options = {"bootstrap_token": self.bootstrap} if setup else {}
        return TestClient(create_app(workspace=self.workspace, **options),
                          base_url=ORIGIN, follow_redirects=False)

    def exchange(self, client):
        response = client.get("/setup/exchange", params={"token": self.bootstrap})
        self.assertEqual((response.status_code, response.headers.get("location")), (303, "/setup"))
        self.assertTrue("HttpOnly" in response.headers["set-cookie"])
        self.assertTrue("SameSite=strict" in response.headers["set-cookie"])
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
        self.assertTrue(self.bootstrap not in response.text + str(response.headers))

    def initialize_cli(self, workspace):
        proc = subprocess.run(
            [sys.executable, "-m", "repairlib", "setup", "--workspace", str(workspace),
             "--shop", FIELDS["shop"], "--timezone", FIELDS["timezone"],
             "--username", FIELDS["username"]],
            input=PASSWORD + "\n", capture_output=True, text=True, env=python_env(), timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def authenticate(self, client, username=FIELDS["username"], password=PASSWORD):
        form = client.get("/login")
        self.assertIsNotNone(csrf(form))
        return client.post("/login", data={
            "username": username, "password": password, "csrf": csrf(form),
        })

    def snapshot(self, database=None):
        with closing(sqlite3.connect(database or self.database)) as db:
            return (
                db.execute("SELECT name FROM shops").fetchall(),
                db.execute("SELECT shop_id, timezone FROM shop_settings").fetchall(),
                db.execute("SELECT shop_id, username, role, active FROM users").fetchall(),
                db.execute("SELECT shop_id, actor_kind, operation FROM audit_events").fetchall(),
                db.execute("SELECT COUNT(*) FROM sessions").fetchone(),
            )

    def test_browser_cli_setup_parity_restart_and_logout(self):
        with self.client(setup=True) as client:
            self.assertEqual(client.get("/setup").status_code, 403)
            self.exchange(client)
            self.assertEqual(client.get("/setup/exchange", params={"token": self.bootstrap}).status_code, 403)
            page = client.get("/setup")
            self.assertIsNotNone(csrf(page))
            self.assertEqual(page.headers.get("cache-control"), "no-store")
            response = client.post("/setup", data={**FIELDS, "password": PASSWORD, "csrf": csrf(page)})
            self.assertEqual((response.status_code, response.headers.get("location")), (303, "/"))
            self.assertEqual(client.get("/setup").status_code, 404)
            self.assertIn("Restart", client.get("/").text)
            self.assertFalse(client.get("/health").json()["ready"])
        cli_workspace = self.workspace.parent / "cli"
        self.initialize_cli(cli_workspace)
        self.assertEqual(self.snapshot(), self.snapshot(cli_workspace / "state" / self.database.name))

        with self.client() as client:
            self.assertTrue(client.get("/health").json()["ready"])
            response = self.authenticate(client)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(any("HttpOnly" in value and "SameSite=strict" in value
                                for value in response.headers.get_list("set-cookie")))
            self.assertTrue(PASSWORD not in response.text + str(response.headers))
            account = client.get("/account")
            self.assertIn("owner", account.text)
            cookies = dict(client.cookies)
        with self.client() as client:
            client.cookies.update(cookies)
            account = client.get("/account")
            self.assertIsNotNone(csrf(account))
            before = self.snapshot()
            self.assertEqual(client.post("/logout").status_code, 403)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(client.post("/logout", data={"csrf": csrf(account)}).status_code, 303)
            client.cookies.update(cookies)
            self.assertEqual(client.get("/").headers.get("location"), "/login")
            self.assertEqual(self.snapshot()[-1], (0,))

    def test_setup_refuses_csrf_expired_grant_and_invalid_fields(self):
        with self.client(setup=True) as client:
            self.exchange(client)
            data = {**FIELDS, "password": PASSWORD, "csrf": csrf(client.get("/setup"))}
            self.assertEqual(client.post("/setup", data={**data, "csrf": "invalid"}).status_code, 403)
            self.assertEqual(client.post("/setup", data=data, headers={
                "Origin": "http://127.0.0.1:9000",
            }).status_code, 403)
            invalid = client.post("/setup", data={**data, "timezone": "bad zone"})
            self.assertEqual(invalid.status_code, 422)
            self.assertIn("Synthetic &lt;Shop&gt;", invalid.text)
            self.assertTrue(PASSWORD not in invalid.text)
            self.assertFalse(self.workspace.exists())
            with patch("repairlib.adapters.web.security.monotonic", return_value=10**12):
                self.assertEqual(client.post("/setup", data=data).status_code, 403)
            self.assertFalse(self.workspace.exists())

    def test_browser_and_cli_setup_race_publish_once(self):
        from repairlib.lifecycle.workspace import publish_state

        publishing = Event()
        resume = Event()

        def pause_before_publish(stage, active):
            publishing.set()
            if not resume.wait(10):
                raise TimeoutError("Timed out waiting for the competing setup")
            publish_state(stage, active)

        with self.client(setup=True) as client, ThreadPoolExecutor() as executor:
            self.exchange(client)
            with patch("repairlib.lifecycle.workspace.publish_state", side_effect=pause_before_publish):
                web = executor.submit(client.post, "/setup", data={
                    **FIELDS, "shop": "Browser shop", "password": PASSWORD,
                    "csrf": csrf(client.get("/setup")),
                })
                try:
                    self.assertTrue(publishing.wait(10))
                    proc = subprocess.run([
                        sys.executable, "-m", "repairlib", "setup", "--workspace", str(self.workspace),
                        "--shop", "CLI shop", "--timezone", "UTC", "--username", "owner",
                    ], input=PASSWORD + "\n", capture_output=True, text=True, env=python_env(), timeout=10)
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn("maintenance is in progress", proc.stderr)
                    self.assertFalse(self.database.exists())
                finally:
                    resume.set()
                self.assertEqual(web.result(timeout=10).status_code, 303)
            self.assertEqual(self.snapshot()[0], [("Browser shop",)])
            self.assertEqual(self.snapshot()[3], [(1, "maintenance", "setup")])
            self.assertEqual(client.get("/setup").status_code, 404)

    def test_login_refuses_invalid_inactive_and_other_shop_without_writes(self):
        initialize_workspace(self.workspace, **FIELDS, password=PASSWORD)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other')")
            for shop_id, username, active in ((2, "other", 1), (1, "inactive", 0)):
                db.execute(
                    "INSERT INTO users(shop_id, username, role, password_hash, active) "
                    "SELECT ?, ?, role, password_hash, ? FROM users WHERE username = 'owner'",
                    (shop_id, username, active),
                )
        with self.client() as client:
            before = self.snapshot()
            responses = [self.authenticate(client, username, password) for username, password in (
                ("other", PASSWORD), ("inactive", PASSWORD), ("owner", "wrong"),
            )]
            for response in responses:
                self.assertEqual(response.status_code, 401)
                self.assertIn("Username or password is incorrect.", response.text)
            self.assertEqual(client.post("/login", data={
                "username": "owner", "password": PASSWORD, "csrf": "invalid",
            }).status_code, 403)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.authenticate(client).status_code, 303)
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE users SET active = 0, revision = revision + 1 WHERE username = 'owner'")
            self.assertEqual(client.get("/").headers.get("location"), "/login")

    def test_dev_prints_setup_link_only_to_terminal_and_disables_access_log(self):
        from io import StringIO

        from repairlib.__main__ import main

        args = ["dev", "--workspace", str(self.workspace), "--browser-setup"]
        with patch("sys.stdout", new_callable=StringIO) as out, patch("uvicorn.run") as run:
            with patch("sys.stderr", new_callable=StringIO):
                self.assertEqual(main(args), 1)
            run.assert_not_called()
            self.assertEqual(out.getvalue(), "")
            with patch.object(out, "isatty", return_value=True):
                self.assertEqual(main(args), 0)
            match = re.search(r"http://127\.0\.0\.1:8000/setup/exchange\?token=([A-Za-z0-9_-]+)",
                              out.getvalue())
            self.assertTrue(match is not None)
            self.assertFalse(run.call_args.kwargs["access_log"])
            self.assertFalse(run.call_args.kwargs["proxy_headers"])
            self.assertEqual(run.call_args.kwargs["ws"], "none")
            with TestClient(run.call_args.args[0], base_url=ORIGIN) as client:
                self.assertEqual(client.get("/setup/exchange", params={"token": match[1]}).status_code, 200)
            self.assertFalse(self.workspace.exists())

    def test_host_request_limits_rate_limit_and_security_headers(self):
        initialize_workspace(self.workspace, **FIELDS, password=PASSWORD)
        with self.client() as client:
            self.assertEqual(client.get("/health", headers={
                "Host": "untrusted.example", "X-Forwarded-Host": "127.0.0.1",
            }).status_code, 400)
            self.assertEqual(client.get("/health", headers={"Host": "[::1]:8000"}).status_code, 200)
            self.assertEqual(client.get("/health", headers={"Host": "[::1].example:8000"}).status_code, 400)
            duplicate_hosts = [("Host", "localhost"), ("Host", "untrusted.example")]
            self.assertEqual(client.get("/health", headers=duplicate_hosts).status_code, 400)
            form = client.get("/login")
            self.assertIn("frame-ancestors 'none'", form.headers.get("content-security-policy", ""))
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            self.assertEqual(client.post("/login", content="x" * 20000, headers=headers).status_code, 413)
            self.assertEqual(client.post("/login", content=iter([b"x" * 9000, b"y" * 9000]),
                                         headers=headers).status_code, 413)
            self.assertEqual(client.post("/login", json={"password": PASSWORD}).status_code, 415)
            self.assertEqual(client.post("/login", content="csrf=a&csrf=b",
                                         headers=headers).status_code, 400)
            before = self.snapshot()
            with patch("repairlib.adapters.web.routes.login", return_value=None) as verify:
                for _ in range(5):
                    self.assertEqual(self.authenticate(client, password="wrong").status_code, 401)
                refused = self.authenticate(client)
                self.assertEqual(refused.status_code, 429)
                self.assertGreater(int(refused.headers["retry-after"]), 0)
                self.assertEqual(verify.call_count, 5)
            self.assertEqual(self.snapshot(), before)
            with patch("repairlib.adapters.web.security.monotonic", return_value=10**12):
                self.assertEqual(self.authenticate(client).status_code, 303)
