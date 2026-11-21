"""Failure visibility: request IDs, logs, error envelopes, SQLite settings, stable forms."""

from contextlib import ExitStack, closing, redirect_stderr
from io import StringIO
import json
import logging
from logging.config import DictConfigurator
import secrets
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from uvicorn.config import LOGGING_CONFIG

from repairlib.adapters.web.security import SESSION_COOKIE
from repairlib.app import create_app
from repairlib.lifecycle.workspace import WorkspaceError
from repairlib.storage import StorageOperationalError
from repairlib.storage.sessions import connect
from test_baseline import python_env
from test_tokens import ACCOUNT, ORIGIN, PASSWORD, TokenFixture, hidden, issued_secret

DASHBOARD = "repairlib.adapters.api.routes.read_dashboard"
DASHBOARD_QUERY = "repairlib.domain.dashboard.dashboard.repair_queue"
FAILURE_LOG = "uvicorn.error.repairlib"


class OperabilityTests(TokenFixture, unittest.TestCase):
    def quiet_client(self):
        return TestClient(create_app(workspace=self.workspace), base_url=ORIGIN,
                          follow_redirects=False, raise_server_exceptions=False)

    def token(self, client):
        self.sign_in(client)
        return issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))

    def test_unexpected_api_failure_is_logged_with_request_id_and_no_secret(self):
        with self.quiet_client() as client:
            token = self.token(client)
            session = client.cookies.get(SESSION_COOKIE)
            assert session is not None
            wrong = "rl_" + secrets.token_urlsafe(32)
            raw_query = "probe=synthetic-private-query"
            bogus_host = "[synthetic-private-host.invalid"
            path = f"/api/v1/dashboard?{raw_query}"
            with ExitStack() as stack:
                stderr = stack.enter_context(redirect_stderr(StringIO()))
                config = DictConfigurator(LOGGING_CONFIG)
                config.config["formatters"]["default"] = config.configure_formatter(
                    config.config["formatters"]["default"],
                )
                handler = config.configure_handler(config.config["handlers"]["default"])
                error_logger = logging.getLogger("uvicorn.error")
                stack.callback(error_logger.setLevel, error_logger.level)
                error_logger.setLevel(logging.INFO)
                with (
                    patch.multiple(logging.getLogger("uvicorn"), handlers=[handler], propagate=False),
                    patch.multiple(error_logger, handlers=[], propagate=True),
                    patch(DASHBOARD_QUERY, side_effect=RuntimeError(f"synthetic exception text: {token}")),
                ):
                    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
                    wrong_auth = client.get(path, headers={"Authorization": f"Bearer {wrong}"})
                    absent = client.get(path)
                    invalid_host = client.get(path, headers={"Host": bogus_host, "Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 500)
        error = response.json()["error"]
        self.assertEqual(error["code"], "server_error")
        self.assertEqual(error["request_id"], response.headers["x-request-id"])
        logged = stderr.getvalue()
        lines = logged.splitlines()
        self.assertEqual(len(lines), 4)
        for reply, status, attribution in (
            (response, 500, "shop_id=1 actor_id=1"),
            (wrong_auth, 401, "shop_id=None actor_id=None"),
            (absent, 401, "shop_id=None actor_id=None"),
            (invalid_host, 400, "shop_id=None actor_id=None"),
        ):
            self.assertEqual(reply.status_code, status)
            line, = [entry for entry in lines if f'request_id={reply.headers["x-request-id"]}' in entry]
            self.assertIn(f"status={status}", line)
            self.assertIn(attribution, line)
        self.assertIn("operation='/api/v1/dashboard'", lines[0])
        self.assertIn("RuntimeError", lines[0])
        self.assertIn("operation='unmatched'", lines[-1])
        self.assertIn("code=invalid_host", lines[-1])
        for private in (token, session, wrong, raw_query, bogus_host, "synthetic exception text",
                        "Workshop CLI", "owner", "Synthetic"):
            self.assertTrue(private not in logged and private not in response.text)

    def test_busy_database_returns_503_and_refusals_are_logged(self):
        with self.quiet_client() as client:
            token = self.token(client)
            auth = {"Authorization": f"Bearer {token}"}
            with patch(DASHBOARD, side_effect=StorageOperationalError("database is locked")), \
                    self.assertLogs(FAILURE_LOG) as busy_logs:
                busy = client.get("/api/v1/dashboard", headers=auth)
            self.assertEqual(busy.status_code, 503)
            self.assertEqual(busy.json()["error"]["code"], "busy")
            busy_line, = busy_logs.output
            self.assertIn("code=busy shop_id=None actor_id=None stack=None", busy_line)
            with self.assertLogs(FAILURE_LOG, "INFO") as logs:
                missing = client.get("/api/v1/customers/999", headers=auth)
        self.assertEqual(missing.status_code, 404)
        request_id = missing.json()["error"]["request_id"]
        self.assertEqual(request_id, missing.headers["x-request-id"])
        line, = [entry for entry in logs.output if request_id in entry]
        self.assertIn("404", line)
        self.assertIn("not_found", line)
        self.assertIn("shop_id=1 actor_id=1", line)
        self.assertNotIn(token, line)

    def test_unexpected_browser_failure_shows_an_html_error_page(self):
        with self.quiet_client() as client:
            self.sign_in(client)
            with patch(DASHBOARD_QUERY, side_effect=RuntimeError("synthetic failure")), \
                    self.assertLogs(FAILURE_LOG) as logs:
                response = client.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn(response.headers["x-request-id"], response.text)
        self.assertNotIn("synthetic failure", response.text)
        self.assertNotIn("Traceback", response.text)
        line, = logs.output
        self.assertIn("shop_id=1 actor_id=1", line)

    def test_token_listing_failure_after_session_check_is_attributed(self):
        with self.quiet_client() as client:
            self.sign_in(client)
            with patch("repairlib.domain.tokens.tokens.list_owned", side_effect=RuntimeError("private")), \
                    self.assertLogs(FAILURE_LOG) as logs:
                reply = client.get(ACCOUNT)
        self.assertEqual(reply.status_code, 500)
        line, = logs.output
        self.assertIn("shop_id=1 actor_id=1", line)

    def test_listener_state_failure_uses_sanitized_pre_response_handler(self):
        def listener_state() -> dict:
            raise RuntimeError("private listener text")

        app = create_app(workspace=self.workspace, listener_state=listener_state)
        with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
            with self.assertLogs(FAILURE_LOG) as logs:
                reply = client.get("/api/v1/dashboard")
        self.assertEqual(reply.status_code, 500)
        self.assertEqual(reply.json()["error"]["code"], "server_error")
        line, = logs.output
        self.assertIn(reply.headers["x-request-id"], line)
        self.assertNotIn("private listener text", line)

    def test_update_rehearsal_requires_reserve_in_addition_to_payload_headroom(self):
        from types import SimpleNamespace

        from repairlib.lifecycle.update import check_space

        candidate = self.workspace.parent / "candidate"
        candidate.mkdir()
        payload = sum(path.stat().st_size for path in self.db.parent.iterdir() if path.is_file())
        required = (1 << 30) + 3 * payload
        with patch("repairlib.lifecycle.update.shutil.disk_usage", return_value=SimpleNamespace(free=required - 1)):
            with self.assertRaisesRegex(WorkspaceError, "Insufficient free space"):
                check_space(self.workspace, candidate)
        with patch("repairlib.lifecycle.update.shutil.disk_usage", return_value=SimpleNamespace(free=required)) as usage:
            check_space(self.workspace, candidate)
            usage.assert_called_once_with(candidate)
        self.assertEqual(list(candidate.iterdir()), [])

    def test_sqlite_connections_set_durability_and_busy_timeout(self):
        with closing(connect(self.db, "rw")) as db:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone(), (2,))
            self.assertGreaterEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone(), (1,))

    def test_cli_reports_sqlite_settings(self):
        def run(*args):
            return subprocess.check_output(
                [sys.executable, "-B", "-m", "repairlib", *args,
                 "--workspace", str(self.workspace)], env=python_env(), text=True, timeout=15,
            )

        expected = "SQLite: synchronous=FULL, busy timeout=5000 ms"
        self.assertIn(expected, run("doctor"))
        self.assertIn(expected, run("status"))
        self.assertEqual(json.loads(run("status", "--json"))["sqlite"],
                         {"synchronous": "FULL", "busy_timeout_ms": 5000})

    def test_startup_refuses_a_symlinked_form_key(self):
        target = self.workspace / "other-file"
        target.write_bytes(b"x" * 32)
        target.chmod(0o600)
        (self.workspace / ".form-key").symlink_to(target)
        with self.assertRaisesRegex(WorkspaceError, "form key"), self.client():
            pass
        self.assertEqual(target.read_bytes(), b"x" * 32)

    def test_forms_stay_valid_across_an_app_restart(self):
        key_file = self.workspace / ".form-key"
        with self.client() as first:
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
            form = first.get("/login")
            cookies = dict(first.cookies)
        with self.client() as second:
            second.cookies.update(cookies)
            reply = second.post("/login", data={"username": "owner", "password": PASSWORD,
                                                "csrf": hidden(form, "csrf")})
        self.assertEqual(reply.status_code, 303)


if __name__ == "__main__":
    unittest.main()
