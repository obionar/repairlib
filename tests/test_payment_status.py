"""Payment status (paid / not paid) through the API, CLI and MCP, not only the web ticket."""

import asyncio
from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_mcp_reads import MCPFixture
from test_repairs import API, DEVICE, PASSWORD
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_users import hidden


class PaymentApiCliTests(TokenFixture, unittest.TestCase):
    def test_api_and_cli_set_and_read_payment_with_refusals(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                       "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
            db.execute("INSERT INTO repairs(id,shop_id,reported_issue,stage) VALUES (50,2,'Hidden','repaired')")
        with self.client() as owner, self.client() as viewer:
            self.sign_in(owner)
            auth = {"Authorization": f"Bearer {issued_secret(owner.post(ACCOUNT, data=self.issue_form(owner)))}"}

            def post(path, body, *, key=None, headers=auth):
                return owner.post(API + path, json=body, headers=headers | {"Idempotency-Key": key or uuid4().hex})

            repair = post("", DEVICE).json()
            self.assertIsNone(repair["payment_paid"])
            path = f"/{repair['id']}/payment"
            early = post(path, {"revision": 1, "enabled": True})
            self.assertEqual((early.status_code, early.json()["error"]["code"]), (409, "finish_repair"))
            self.assertEqual(post(f"/{repair['id']}/stage", {"revision": 1, "stage": "no_repair"}).status_code, 200)
            self.assertEqual(post(path, {"revision": 1, "enabled": True}).status_code, 409)  # Stale.
            self.assertEqual(post("/50/payment", {"revision": 1, "enabled": True}).status_code, 404)
            self.sign_in(viewer, "viewer")
            viewer_token = issued_secret(viewer.post(ACCOUNT, data={
                "csrf": hidden(viewer.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Viewer read", "scope": "read", "password": PASSWORD}))
            denied = post(path, {"revision": 2, "enabled": True}, headers={"Authorization": f"Bearer {viewer_token}"})
            self.assertEqual(denied.status_code, 403)
            self.assertIsNone(owner.get(API + f"/{repair['id']}", headers=auth).json()["payment_paid"])
            key = uuid4().hex
            paid = post(path, {"revision": 2, "enabled": True}, key=key)
            self.assertEqual(paid.status_code, 200, paid.text)
            self.assertEqual((paid.json()["payment_paid"], paid.json()["revision"]), (True, 3))
            self.assertEqual(post(path, {"revision": 2, "enabled": True}, key=key).json(), paid.json())
            self.assertIs(owner.get(API + f"/{repair['id']}", headers=auth).json()["payment_paid"], True)

        def cli(*args, code=0):
            result = subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                                    text=True, capture_output=True, timeout=15, env=python_env())
            self.assertEqual(result.returncode, code, result.stderr)
            return json.loads(result.stdout) if code == 0 else None

        self.assertIs(cli("show", str(repair["id"]))["payment_paid"], True)
        unpaid = cli("payment", str(repair["id"]), "--revision", "3", "--undo", "--key", uuid4().hex)
        self.assertEqual((unpaid["payment_paid"], unpaid["revision"]), (False, 4))
        cli("payment", str(repair["id"]), "--revision", "3", "--key", uuid4().hex, code=7)
        audit = self.snapshot("SELECT source, changes FROM audit_events WHERE operation='repair.payment' ORDER BY id")
        self.assertEqual([(source, json.loads(changes)) for source, changes in audit],
                         [("api", {"payment": {"before": None, "after": True}}),
                          ("cli", {"payment": {"before": True, "after": False}})])
        self.assertEqual(self.snapshot("SELECT payment_paid FROM repairs WHERE id=50"), [(None,)])


class PaymentMcpTests(MCPFixture, unittest.TestCase):
    def test_mcp_marks_paid_and_unpaid(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api, name="Payment writer")
            profile = self.profile(api, token)

            async def workflow():
                async with self.session(profile, token) as session:
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    for name in ("mark_repair_paid", "mark_repair_unpaid"):
                        self.assertEqual(set(tools[name].input_schema["required"]), {"repair_id", "revision", "key"})
                        self.assertTrue(tools[name].annotations.destructive_hint)

                    async def call(name, **arguments):
                        return await self.call(session, name, token, **arguments)

                    repair = await call("create_repair", key="repair", reported_issue="Synthetic device")
                    ref = {"repair_id": repair["id"]}
                    early = await call("mark_repair_paid", error="finish_repair", **ref, revision=1, key="early")
                    self.assertEqual(early["http_status"], 409)
                    await call("change_repair_stage", **ref, revision=1, stage="no_repair", key="stage")
                    paid = await call("mark_repair_paid", **ref, revision=2, key="paid")
                    self.assertIs(paid["payment_paid"], True)
                    self.assertIs((await call("get_repair", **ref))["payment_paid"], True)
                    unpaid = await call("mark_repair_unpaid", **ref, revision=3, key="unpaid")
                    self.assertIs(unpaid["payment_paid"], False)

            asyncio.run(workflow())
        self.assertEqual(self.snapshot(
            "SELECT source FROM audit_events WHERE operation='repair.payment' ORDER BY id"), [("mcp",), ("mcp",)])


if __name__ == "__main__":
    unittest.main()
