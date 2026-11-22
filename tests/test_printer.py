"""P15 lifecycle through authenticated adapters, using a disposable executable."""

from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
import test_printing
from test_repairs import API, CUSTOMERS, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret


SETTINGS = "/api/v1/settings/printer"


def fake_p15(root):
    binary = root / "p15"
    binary.write_text(f"""#!{sys.executable}
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
with (root / 'calls').open('a') as log:
    print(json.dumps(sys.argv[1:]), file=log, flush=True)
mode = (root / 'mode').read_text()
if mode == 'hang':
    time.sleep(15)
if mode == 'fail':
    sys.stderr.write('private driver output' * 100_000)
    sys.exit(1)
""")
    binary.chmod(0o700)
    (root / "mode").write_text("ok")
    return binary


class PrinterTests(TokenFixture, TestCase):
    admin_auth = test_printing.PrintingTests.admin_auth

    def setUp(self):
        super().setUp()
        self.root = self.workspace.parent
        self.binary = fake_p15(self.root)
        env = patch.dict(os.environ, {"PATH": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def write(self, client, auth, path, body, *, key=None, method="POST"):
        return client.request(method, path, json=body,
                              headers={**auth, "Idempotency-Key": key or uuid4().hex})

    def test_inventory_label_single_attempt_and_revision(self):
        from test_tokens import hidden

        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.write(client, auth, SETTINGS, {"driver": "p15", "revision": 1}, method="PATCH")
            item = self.write(client, auth, "/api/v1/inventory",
                              {"sku": "--part", "description": "Synthetic component",
                               "selling_price_cents": 0}).json()
            page = client.get(f"/inventory/{item['id']}")
            self.assertIn("Send to P15", page.text)
            form = {name: hidden(page, name) for name in ("csrf", "key", "revision", "printer_revision")}
            path = f"/inventory/{item['id']}/label"
            self.assertEqual(client.post(path, data={**form, "csrf": "bad"}).status_code, 403)
            self.assertEqual(client.post(path, data={**form, "revision": "9"}).status_code, 409)
            self.assertEqual(client.post(path, data=form).status_code, 200)
            self.assertEqual(client.post(path, data=form).status_code, 200)
            self.assertEqual(self.calls(), [["inv", "-n", "1", "--", "--part", "Synthetic component"]])

    def test_settings_and_label_guards_replay_and_uncertainty(self):
        with self.client() as client, self.client() as viewer:
            _, auth = self.admin_auth(client)
            current = client.get(SETTINGS, headers=auth)
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.json(), {"driver": "none", "revision": 1, "available": True})
            customer = self.write(client, auth, CUSTOMERS, {"name": "--copies=500; $(exit)"}).json()
            repair = self.write(client, auth, API, {**DEVICE, "customer_id": customer["id"]}).json()
            label = f"{API}/{repair['id']}/label"
            self.assertEqual(self.write(client, auth, label, {"revision": 1, "printer_revision": 1})
                             .json()["error"]["code"], "printer_disabled")
            self.assertEqual(self.write(client, auth, SETTINGS, {"driver": "/bin/sh", "revision": 1},
                                        method="PATCH").status_code, 422)
            self.assertEqual(self.write(client, auth, SETTINGS,
                                        {"driver": "p15", "revision": 1, "shop_id": 2},
                                        method="PATCH").status_code, 422)
            self.assertEqual(self.write(client, auth, SETTINGS, {"driver": "p15", "revision": 2},
                                        method="PATCH").status_code, 409)
            enable = {"driver": "p15", "revision": 1}
            updated = self.write(client, auth, SETTINGS, enable, method="PATCH", key="enable-p15")
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["revision"], 2)
            self.assertEqual(self.write(client, auth, SETTINGS, enable,
                                        method="PATCH", key="enable-p15").json(), updated.json())

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO shop_settings(shop_id,timezone) VALUES (2,'UTC')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
            self.sign_in(viewer, "viewer")
            token = issued_secret(viewer.post(ACCOUNT, data=self.issue_form(viewer, name="Viewer", scope="read")))
            view_auth = {"Authorization": f"Bearer {token}"}
            details = {"revision": repair["revision"], "printer_revision": 2}
            self.assertEqual(client.get(SETTINGS, headers=view_auth).status_code, 403)
            self.assertEqual(self.write(client, view_auth, SETTINGS, enable, method="PATCH").status_code, 403)
            self.assertEqual(self.write(client, view_auth, SETTINGS + "/test", {"revision": 2}).status_code, 403)
            self.assertEqual(client.get(label, headers=view_auth).status_code, 403)
            self.assertEqual(self.write(client, view_auth, label, details).status_code, 403)
            self.assertEqual(self.write(client, auth, f"{API}/50/label", details).status_code, 404)
            self.assertEqual(self.write(client, auth, label, {**details, "revision": 50}).status_code, 409)
            self.assertEqual(viewer.get(f"/repairs/{repair['id']}/label").status_code, 200)
            self.assertNotIn("Send to P15", viewer.get(f"/repairs/{repair['id']}/label").text)
            self.assertEqual(client.post(f"/repairs/{repair['id']}/label", data={}).status_code, 403)
            self.assertEqual(viewer.get("/settings/printer").status_code, 403)
            self.assertEqual(self.calls(), [])

            self.binary.rename(self.root / "offline")
            self.assertFalse(client.get(SETTINGS, headers=auth).json()["available"])
            self.assertEqual(self.write(client, auth, label, details).json()["error"]["code"],
                             "printer_unavailable")
            (self.root / "offline").rename(self.binary)
            self.assertEqual(self.calls(), [])
            self.assertEqual(self.snapshot("SELECT count(*) FROM audit_events "
                                           "WHERE operation LIKE 'printer.%' AND operation != 'printer.configure'"), [(0,)])
            self.assertEqual(self.snapshot("SELECT printer_driver, printer_revision FROM shop_settings "
                                           "WHERE shop_id=2"), [("none", 1)])

            test = self.write(client, auth, SETTINGS + "/test", {"revision": 2}, key="test-p15")
            self.assertEqual(test.status_code, 200)
            self.assertEqual(test.json()["outcome"], "acknowledged")
            printed = self.write(client, auth, label, details, key="print-p15")
            self.assertEqual(printed.status_code, 200)
            self.assertEqual(printed.json()["outcome"], "acknowledged")
            self.assertEqual(self.calls(), [["text", "-n", "1", "--", "RepairLib test"],
                                            ["repair", "-n", "1", "--", str(repair["id"]),
                                             "--copies=500; $(exit)"]])

            (self.root / "mode").write_text("fail")
            failed = self.write(client, auth, label, details, key="print-failed")
            self.assertEqual(failed.json()["outcome"], "failed")
            self.assertIn("may already have reached paper", failed.json()["message"])
            self.assertNotIn("private driver output", failed.text)

            (self.root / "mode").write_text("hang")
            with patch("repairlib.domain.printing.P15_TIMEOUT", 0.5):
                timed_out = self.write(client, auth, label, details, key="print-timeout")
            self.assertEqual(timed_out.json()["outcome"], "uncertain")
            self.assertIn("may already have reached paper", timed_out.json()["message"])
            self.assertEqual(len(self.calls()), 4)

            # A failed final audit write rolls back the terminal receipt while
            # the preceding attempt remains irrevocably reserved.
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("""
                    CREATE TRIGGER reject_print_receipt BEFORE INSERT ON audit_events
                    WHEN NEW.operation = 'printer.label'
                    BEGIN SELECT RAISE(ABORT, 'synthetic unavailable audit'); END
                    """)
            (self.root / "mode").write_text("ok")
            lost = self.write(client, auth, label, details, key="print-lost")
            self.assertEqual(lost.json(), timed_out.json())
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("DROP TRIGGER reject_print_receipt")
            self.assertEqual(len(self.calls()), 5)

        with self.client() as restarted:
            self.assertEqual(restarted.get(SETTINGS, headers=auth).json(), updated.json())
            for key, receipt in (("print-timeout", timed_out), ("print-failed", failed),
                                 ("print-p15", printed), ("print-lost", lost)):
                self.assertEqual(self.write(restarted, auth, label, details, key=key).json(), receipt.json())
            self.assertEqual(self.write(restarted, auth, label, {**details, "revision": 9},
                                        key="print-p15").status_code, 409)
        self.assertEqual(len(self.calls()), 5)
        self.assertEqual(self.snapshot(
            "SELECT count(*) FROM audit_events WHERE operation = 'printer.label.requested'"), [(4,)])
        self.assertEqual(self.snapshot(
            "SELECT shop_id,actor_username,source,operation,entity_id,json_extract(changes,'$.outcome') "
            "FROM audit_events WHERE operation IN ('printer.test','printer.label') ORDER BY id"),
            [(1, "owner", "api", "printer.test", 1, "acknowledged"),
             (1, "owner", "api", "printer.label", repair["id"], "acknowledged"),
             (1, "owner", "api", "printer.label", repair["id"], "failed"),
             (1, "owner", "api", "printer.label", repair["id"], "uncertain")])

    def test_real_http_cli_printer_lifecycle(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)

            def cli(*args, stdin=None):
                result = subprocess.run([*COMMAND, *args], input=stdin, text=True, capture_output=True,
                                        timeout=10, env=python_env())
                self.assertTrue(token not in result.stdout + result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout) if "--json" in args else None

            opts = ("--workspace", str(self.workspace), "--json")
            self.assertEqual(cli("printer", "show", *opts)["driver"], "none")
            self.assertEqual(cli("printer", "set", "p15", "--revision", "1",
                                 "--key", "cli-p15-set", *opts)["revision"], 2)
            self.assertEqual(cli("printer", "test", "--revision", "2", "--key", "cli-p15-test",
                                 *opts)["outcome"], "acknowledged")
            repair = self.write(client, auth, API, DEVICE).json()
            self.assertEqual(cli("repairs", "label-options", str(repair["id"]), *opts), {
                "revision": repair["revision"], "printer_revision": 2, "enabled": True, "available": True,
            })
            self.assertEqual(cli("repairs", "print-label", str(repair["id"]),
                                 "--revision", str(repair["revision"]), "--printer-revision", "2",
                                 "--key", "cli-p15-label", *opts)["outcome"], "acknowledged")
            self.assertEqual(self.calls()[-1], ["repair", "-n", "1", "--", str(repair["id"]), ""])
