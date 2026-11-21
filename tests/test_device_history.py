"""Device history reads reach the CLI and MCP with the API's shop-scoped rows."""

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
from test_tokens import ACCOUNT, TokenFixture, issued_secret

API = "/api/v1/repairs"
SERIAL = "SN-2024-001"
OUTSIDE = "SN-OUTSIDE-002"


class DeviceHistoryFixture(TokenFixture):
    reuse_workspace = True

    def auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def create(self, client, auth, **fields):
        response = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"reported_issue": "Synthetic issue", **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def seed(self, client, auth):
        newer = self.create(client, auth, serial=SERIAL, reported_issue="Later issue")
        older = self.create(client, auth, serial=SERIAL, reported_issue="Earlier issue")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE repairs SET created_at = ? WHERE id = ?",
                       ("2020-01-01T10:00:00.000Z", older["id"]))
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other synthetic shop')")
            db.execute("INSERT INTO repairs(shop_id, serial, reported_issue) VALUES (2, ?, ?)",
                       (OUTSIDE, "Outside issue"))
            db.execute("INSERT INTO repairs(shop_id, serial, reported_issue) VALUES (2, ?, ?)",
                       (SERIAL, "Outside same serial"))
        return older, newer

    def rows(self, older, newer):
        return [
            {"id": older["id"], "created_at": "2020-01-01T10:00:00.000Z",
             "stage": "new", "reported_issue": "Earlier issue"},
            {name: newer[name] for name in ("id", "created_at", "stage", "reported_issue")},
        ]


class DeviceHistoryCLITests(DeviceHistoryFixture, unittest.TestCase):
    def test_cli_lists_the_shop_serial_oldest_first(self):
        with self.client() as client:
            older, newer = self.seed(client, self.auth(client))
        expected = self.rows(older, newer)

        def cli(*args):
            result = subprocess.run(
                [*COMMAND, "repairs", "device-history", *args, "--workspace", str(self.workspace)],
                env=python_env(), text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertEqual(json.loads(cli("--serial", SERIAL, "--json")), expected)
        self.assertEqual(cli("--serial", SERIAL).splitlines(), [
            f"{row['id']}\t{row['created_at']}\t{row['stage']}\t{row['reported_issue']}"
            for row in expected
        ])
        self.assertEqual(json.loads(cli("--serial", OUTSIDE, "--json")), [])


class DeviceHistoryMCPTests(DeviceHistoryFixture, MCPFixture, unittest.TestCase):
    def test_mcp_returns_the_api_rows_and_nothing_from_another_shop(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api)
            auth = {"Authorization": f"Bearer {token}"}
            older, newer = self.seed(api, auth)
            expected = self.rows(older, newer)
            response = api.get(f"{API}/device-history", headers=auth, params={"serial": SERIAL})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), expected)

            async def verify():
                async with self.session(self.profile(api, token), token) as session:
                    rows = await self.call(session, "device_history", token, serial=SERIAL)
                    self.assertEqual(rows, response.json())
                    self.assertNotIn("Outside same serial", json.dumps(rows))
                    self.assertEqual(await self.call(session, "device_history", token, serial=OUTSIDE), [])

            asyncio.run(verify())
