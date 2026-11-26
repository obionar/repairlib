"""LAN listen mode for a closed shop network, and the per-client sign-in limit."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.lifecycle.checkout import service_command, service_target
from repairlib.lifecycle.service import UNIT, foreground_args
from test_tokens import PASSWORD, TokenFixture, hidden

LAN = "http://192.168.1.20:8000"


class LanAccessTests(TokenFixture, unittest.TestCase):
    def lan_client(self, *, lan=True, address="192.168.1.31"):
        return TestClient(create_app(workspace=self.workspace, lan=lan), base_url=LAN,
                          client=(address, 50000), follow_redirects=False)

    def sessions(self) -> int:
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def test_lan_host_is_refused_without_lan_mode(self):
        with self.lan_client(lan=False) as client:
            self.assertEqual(client.get("/login").status_code, 400)

    def test_lan_mode_serves_forms_and_still_refuses_a_foreign_origin(self):
        with self.lan_client() as client:
            form = client.get("/login")
            self.assertEqual(form.status_code, 200)
            data = {"username": "owner", "password": PASSWORD, "csrf": hidden(form, "csrf")}
            foreign = client.post("/login", data=data, headers={"Origin": "http://evil.example"})
            self.assertEqual(foreign.status_code, 403)
            self.assertEqual(self.sessions(), 0)
            signed = client.post("/login", data=data, headers={"Origin": LAN})
            self.assertEqual(signed.status_code, 303)
            self.assertEqual(client.get("/repairs").status_code, 200)

    def test_one_client_is_throttled_while_another_can_sign_in(self):
        app = create_app(workspace=self.workspace, lan=True)

        def attempt(address, password):
            with TestClient(app, base_url=LAN, client=(address, 50000), follow_redirects=False) as client:
                form = client.get("/login")
                return client.post("/login", data={
                    "username": "owner", "password": password, "csrf": hidden(form, "csrf")})

        codes = [attempt("192.168.1.66", "wrong").status_code for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(self.sessions(), 0)
        self.assertEqual(attempt("192.168.1.31", PASSWORD).status_code, 303)
        self.assertEqual(self.sessions(), 1)

    def test_service_unit_carries_the_lan_flag_only_when_asked(self):
        workspace = self.workspace
        self.assertNotIn("--lan", foreground_args(workspace, 8000))
        self.assertEqual(foreground_args(workspace, 8000, lan=True)[-1], "--lan")


class LanUpdateTests(unittest.TestCase):
    def test_update_rediscovers_and_reuses_the_lan_flag(self):
        with TemporaryDirectory() as config:
            unit = Path(config) / "systemd" / "user" / UNIT
            unit.parent.mkdir(parents=True)
            argv = " ".join(("/usr/bin/python3", "-m", "repairlib", *foreground_args(Path("/srv/shop"), 8000, lan=True)))
            unit.write_text(f"[Service]\nExecStart={argv}\n")
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": config}), \
                    patch("repairlib.lifecycle.service.OPENRC_INITD", Path(config) / "init.d"), \
                    patch("repairlib.lifecycle.service.using_openrc", return_value=False):
                target = service_target()
        self.assertEqual(target, (Path("/srv/shop"), 8000, False, True))
        with patch("repairlib.lifecycle.checkout.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"service":"running"}', "")
            service_command(Path("/srv/repo"), "status", target)
        self.assertEqual(run.call_args.args[0][-1], "--lan")


if __name__ == "__main__":
    unittest.main()
