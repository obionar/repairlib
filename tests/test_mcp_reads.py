"""Real SDK stdio sessions on a local workspace, compared with the HTTP API."""

import asyncio
from base64 import b64decode
from contextlib import asynccontextmanager, closing
import json
import secrets
import subprocess
import sqlite3
from tempfile import TemporaryFile
import unittest
from uuid import uuid4

from support.mcp_client import stdio_session
from test_baseline import python_env
from test_charges import PART
from test_cli_identity import COMMAND, workspace_client
from test_inventory_adapters import ITEM
from test_repairs import DEVICE
from test_tokens import ACCOUNT, PASSWORD, TokenFixture, hidden, issued_secret

API = "/api/v1"


class MCPFixture(TokenFixture):
    def token(self, api, username="owner", **options):
        self.sign_in(api, username)
        return issued_secret(api.post(ACCOUNT, data=self.issue_form(api, **options)))

    def profile(self, api, token, name="agent"):
        """MCP needs no credentials; kept so callers read the same."""
        return None

    @asynccontextmanager
    async def session(self, profile, token, *, sensitive=()):
        command = [*COMMAND, "mcp", "--workspace", str(self.workspace)]
        with TemporaryFile(mode="w+") as stderr:
            try:
                async with asyncio.timeout(20):
                    async with stdio_session(command, python_env(), stderr) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.server_info.name, "RepairLib")
                        self.assertIsNotNone(initialized.capabilities.tools)
                        yield session
            finally:
                stderr.seek(0)
                logged = stderr.read()
                self.assertTrue(all(value not in logged for value in (token, *sensitive)),
                                "Sensitive data appeared in stderr")

    async def call(self, session, tool_name, token, *, error=None, **arguments):
        reply = await session.call_tool(tool_name, arguments)
        self.assertTrue(token not in reply.model_dump_json(), "Credential appeared in tool output")
        self.assertEqual(bool(reply.is_error), error is not None, reply)
        if error:
            payload = reply.structured_content["error"]
            self.assertEqual(payload["code"], error)
            return payload
        return reply.structured_content["result"]


class MCPReadTests(MCPFixture, unittest.TestCase):
    def test_stdio_discovery_and_read_parity(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api)
            profile = self.profile(api, token)
            auth = {"Authorization": f"Bearer {token}"}
            # MCP callers are agents, so MCP answers match the API as an agent token sees it.
            agent_auth = {"Authorization": "Bearer " + issued_secret(
                api.post(ACCOUNT, data=self.issue_form(api, name="Agent view", kind="agent")))}

            def post(path, body=None, **kwargs):
                response = api.post(API + path, json=body, **kwargs,
                                    headers={**auth, "Idempotency-Key": uuid4().hex})
                self.assertEqual(response.status_code, 201, response.text)
                return response.json()

            older = post("/customers", {"name": "Older customer"})["id"]
            customer = post("/customers", {"name": "Synthetic customer"})["id"]
            repair = post("/repairs", {**DEVICE, "customer_id": customer})["id"]
            base = f"/repairs/{repair}"
            entry = post(base + "/entries", {"kind": "note", "body": "Synthetic work"})["id"]
            blob = b"Synthetic original attachment\x00\xff"
            attachment = post(base + "/attachments", params={"filename": "note.bin"}, content=blob)["id"]
            oversized = post(base + "/attachments", params={"filename": "large.bin"}, content=b"x" * 65537)["id"]
            charge = post(base + "/charges", PART)["id"]
            item = post("/inventory", ITEM)["id"]
            post(f"/inventory/{item}/receive", {"quantity_milli": 1000, "revision": 1})
            cases = {
                "get_customer": (f"/customers/{customer}", {"customer_id": customer}),
                "customer_history": (f"/customers/{customer}/history", {"customer_id": customer}),
                "list_repairs": ("/repairs?stage=new", {"stage": "new"}),
                "get_repair": (base, {"repair_id": repair}),
                "repair_activity": (base + "/activity", {"repair_id": repair}),
                "list_work_entries": (base + "/entries", {"repair_id": repair}),
                "work_entry_history": (base + f"/entries/{entry}/history", {"repair_id": repair, "entry_id": entry}),
                "list_attachments": (base + "/attachments", {"repair_id": repair}),
                "get_attachment": (base + f"/attachments/{attachment}", {"repair_id": repair, "attachment_id": attachment}),
                "get_charge": (base + f"/charges/{charge}?include_consumption=true", {"repair_id": repair, "charge_id": charge}),
                "get_inventory_item": (f"/inventory/{item}", {"item_id": item}),
                "list_stock_movements": (f"/inventory/{item}/movements", {"item_id": item}),
                "stock_reconciliation": ("/inventory/reconciliation", {}),
                "dashboard": ("/dashboard", {}),
            }

            activity = ("repairs", "activity", str(repair))
            for tool, command in {"repair_activity": activity, "dashboard": ("dashboard",)}.items():
                path, _ = cases[tool]
                result = subprocess.run(
                    [*COMMAND, *command, "--json", "--workspace", str(self.workspace)],
                    env=python_env(), text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                response = api.get(API + path, headers=auth)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(result.stdout), response.json(), tool)

            async def verify():
                async with self.session(profile, token) as session:
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools
                             if tool.annotations.read_only_hint}
                    self.assertEqual(set(tools), set(cases) | {
                        "list_customers", "read_attachment_content", "charge_summary",
                        "part_return_status", "list_inventory", "list_users", "device_history", "repair_brief",
                    })
                    for tool in tools.values():
                        self.assertTrue(tool.description)
                        self.assertTrue(tool.annotations.read_only_hint)
                        self.assertFalse(tool.annotations.destructive_hint)
                        self.assertTrue(tool.annotations.idempotent_hint)
                        self.assertFalse(tool.annotations.open_world_hint)
                        self.assertFalse(tool.input_schema["additionalProperties"])
                        self.assertEqual(tool.output_schema["type"], "object")
                    self.assertEqual(tools["get_repair"].input_schema["required"], ["repair_id"])
                    self.assertEqual(tools["get_repair"].input_schema["properties"]["repair_id"]["type"], "integer")
                    for name, (path, arguments) in cases.items():
                        # One customer's record is an explicit request: MCP shows it whole, like a user token.
                        response = api.get(API + path, headers=auth if name == "get_customer" else agent_auth)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(await self.call(session, name, token, **arguments), response.json(), name)
                    summary = await self.call(session, "charge_summary", token, repair_id=repair)
                    self.assertEqual(summary, api.get(API + base + "/charges?include_consumption=true",
                                                     headers=auth).json())
                    first = await self.call(session, "list_customers", token, limit=1)
                    self.assertEqual(first["next_before"], customer)
                    self.assertEqual(first["items"][0]["open_repair_count"], 1)
                    filtered = await self.call(session, "list_repairs", token, queue="open",
                                               technician="unassigned", customer_id=customer, picked_up=False)
                    self.assertEqual(filtered["total"], 1)
                    self.assertEqual(filtered["items"][0]["customer_name"], f"C{customer}")
                    second = await self.call(session, "list_customers", token, limit=1, before=first["next_before"])
                    self.assertEqual([row["id"] for row in second["items"]], [older])
                    self.assertIsNone(second["next_before"])
                    content = await self.call(session, "read_attachment_content", token,
                                              repair_id=repair, attachment_id=attachment)
                    self.assertEqual(content["attachment"]["id"], attachment)
                    self.assertEqual(content["encoding"], "base64")
                    self.assertEqual(b64decode(content["content_base64"]), blob)
                    await self.call(session, "read_attachment_content", token, error="attachment_too_large",
                                    repair_id=repair, attachment_id=oversized)
                    conflict = await self.call(session, "part_return_status", token, error="not_inventory_linked",
                                               repair_id=repair, charge_id=charge)
                    self.assertEqual(conflict["http_status"], 409)

            asyncio.run(verify())

    def test_stdio_acts_as_mcp_admin_and_refuses_other_shops_and_bad_arguments(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
            for shop in (1, 2):
                db.execute("INSERT INTO inventory_items(id, shop_id, sku, description, unit, purchase_cost_cents) "
                           "VALUES (?, ?, 'CAP', 'Synthetic capacitor', 'pcs', 1847)", (shop, shop))

        async def verify():
            async with self.session(None, "unused-credential") as session:
                inventory = await self.call(session, "list_inventory", "unused-credential",
                                            low_stock=True, query="capacitor")
                self.assertEqual([item["id"] for item in inventory["items"]], [1])
                self.assertEqual(inventory["items"][0]["purchase_cost_cents"], 1847)
                users = await self.call(session, "list_users", "unused-credential")
                self.assertEqual([user["username"] for user in users["items"]], ["owner"])
                for name, code, status, args in (
                    ("get_inventory_item", "not_found", 404, {"item_id": 2}),
                    ("list_inventory", "validation", None, {"shop_id": 2}),
                    ("list_inventory", "validation", None, {"limit": True}),
                    ("list_inventory", "validation", None, {"limit": 101}),
                    ("delete_customer", "unknown_tool", None, {}),
                    ("identity", "unknown_tool", None, {}),
                ):
                    error = await self.call(session, name, "unused-credential", error=code, **args)
                    self.assertEqual(error.get("http_status"), status)

        asyncio.run(verify())
        self.assertEqual(self.snapshot("SELECT username, role FROM users WHERE username = 'mcp'"), [("mcp", "admin")])


