"""Charge and discount workflows through real MCP stdio and authenticated HTTP."""

import asyncio
from contextlib import closing
import sqlite3
import unittest

from test_charges import PART
from test_cli_identity import workspace_client
from test_mcp_mutations import BILLING_TOOLS
from test_mcp_reads import MCPFixture


class MCPChargeTests(MCPFixture, unittest.TestCase):
    def state(self):
        return tuple(self.snapshot(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "repair_charges", "repair_discounts", "charge_requests", "inventory_movements", "audit_events",
        ))

    def test_stdio_charge_and_discount_writes(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id,name) VALUES (2,'Other shop')")
            db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                       "SELECT 2,1,'viewer','viewer',password_hash FROM users WHERE id=1")

        with workspace_client(self.workspace) as (api, _):
            token = self.token(api, name="Billing writer")
            api.cookies.clear()
            viewer = self.token(api, "viewer", scope="read")
            profile = self.profile(api, token)

            async def workflow():
                async with self.session(profile, token) as session:
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    for name, (destructive, review) in BILLING_TOOLS.items():
                        self.assertIn(name, tools.keys())
                        tool = tools[name]
                        required = set(tool.input_schema["required"])
                        self.assertIn("key", required)
                        self.assertEqual(required & {"revision", "version"}, {review} if review else set())
                        self.assertFalse(tool.input_schema["additionalProperties"])
                        self.assertEqual(tool.output_schema["type"], "object")
                        self.assertFalse(tool.annotations.read_only_hint)
                        self.assertEqual(tool.annotations.destructive_hint, destructive)
                        self.assertTrue(tool.annotations.idempotent_hint)
                        self.assertFalse(tool.annotations.open_world_hint)
                    for field in ("quantity_milli", "unit_price_cents"):
                        self.assertEqual(tools["create_charge"].input_schema["properties"][field]["type"], "integer")

                    async def call(name, **arguments):
                        return await self.call(session, name, token, **arguments)

                    repair = await call("create_repair", key="repair", reported_issue="Synthetic device")
                    repair_ref = {"repair_id": repair["id"]}
                    with closing(sqlite3.connect(self.db)) as db, db:
                        outside = db.execute(
                            "INSERT INTO repairs(shop_id, reported_issue) VALUES (2,'Outside repair')"
                        ).lastrowid
                    create = PART | repair_ref | {"key": "charge"}
                    baseline = self.state()
                    await call("create_charge", error="validation", **(create | {"quantity_milli": 1.5}))
                    await call("create_charge", error="validation", **(create | {"unit_price_cents": True}))
                    await call("create_charge", error="not_found", **(create | {"repair_id": outside}))
                    await call("set_discount", error="not_found", repair_id=outside, key="outside", revision=0,
                               kind="amount", value=100)
                    self.assertEqual(self.state(), baseline)

                    path = f"/api/v1/repairs/{repair['id']}/charges"
                    charge = await call("create_charge", **create)
                    committed = self.state()
                    self.assertEqual(await call("create_charge", **create), charge)
                    self.assertEqual(self.state(), committed)
                    self.assertEqual(charge["line_total_cents"], 2999)
                    ref = repair_ref | {"charge_id": charge["id"]}
                    correction = PART | ref | {"key": "correct", "revision": charge["revision"],
                                               "unit_price_cents": 2000, "reason": "Reviewed price"}
                    corrected = await call("correct_charge", **correction)
                    hidden = await call("delete_charge", **ref, key="hide", revision=corrected["revision"],
                                        reason="Review the line")
                    restored = await call("restore_charge", **ref, key="restore", revision=hidden["revision"],
                                          reason="Verified the line")
                    self.assertEqual((restored["revision"], restored["deleted"]), (4, False))
                    discount = repair_ref | {"key": "discount", "revision": 0, "kind": "amount", "value": 100}
                    totals = await call("set_discount", **discount)
                    self.assertEqual((totals["discount_revision"], totals["total_cents"]), (1, 2900))
                    committed = self.state()
                    for name, code, args in (
                        ("correct_charge", "stale_revision", correction | {"key": "stale"}),
                        ("create_charge", "conflict", create | {"unit_price_cents": 1}),
                        ("set_discount", "stale_revision", discount | {"key": "stale", "value": 50}),
                    ):
                        error = await call(name, error=code, **args)
                        self.assertEqual(error["http_status"], 409)
                    self.assertEqual(self.state(), committed)
                    return restored, ref, totals, discount

            charge, ref, totals, discount = asyncio.run(workflow())

        self.assertEqual(self.snapshot(
            "SELECT operation, actor_user_id, source, token_label FROM audit_events "
            "WHERE operation LIKE 'charge.%' OR operation='repair.discount' ORDER BY id"
        ), [(op, 3, "mcp", None) for op in (
            "charge.create", "charge.correct", "charge.delete", "charge.restore",
            "repair.discount",
        )])
        committed = self.state()
        with workspace_client(self.workspace) as (api, _):
            async def readback():
                async with self.session(self.profile(api, token), token) as session:
                    self.assertEqual(await self.call(session, "get_charge", token, **ref), charge)
                    self.assertEqual(await self.call(session, "set_discount", token, **discount), totals)

            asyncio.run(readback())
        self.assertEqual(self.state(), committed)
