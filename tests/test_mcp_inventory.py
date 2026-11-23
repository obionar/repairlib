"""Verify stock and repair consumption writes over real MCP stdio."""

import asyncio
from contextlib import closing
import sqlite3
import unittest

from test_cli_identity import workspace_client
from test_mcp_mutations import STOCK_TOOLS
from test_mcp_reads import MCPFixture


CATALOG = {
    "sku": "MCP-1", "description": "Synthetic fuse", "unit": "pcs", "category": "Fuses",
    "manufacturer": "", "part_number": "", "package": "", "specifications": "", "bin": "",
    "low_stock_milli": 1250, "purchase_cost_cents": 17, "selling_price_cents": 42,
}


class MCPInventoryTests(MCPFixture, unittest.TestCase):
    def inventory_state(self):
        return tuple(self.snapshot(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "inventory_items", "inventory_movements", "inventory_requests", "stock_requests",
            "repairs", "repair_charges", "repair_part_consumptions", "audit_events",
        ))

    def test_stdio_inventory_and_repair_part_workflow(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api, name="Inventory writer")
            profile = self.profile(api, token)
            server = str(api.base_url)

            async def workflow():
                async with self.session(profile, token) as session:
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertEqual(set(STOCK_TOOLS) - tools.keys(), set())
                    for name, (destructive, revisions) in STOCK_TOOLS.items():
                        tool = tools[name]
                        required = set(tool.input_schema["required"])
                        self.assertIn("key", required)
                        self.assertEqual({field for field in required if field.endswith("revision")}, revisions)
                        self.assertFalse(tool.input_schema["additionalProperties"])
                        self.assertEqual(tool.output_schema["type"], "object")
                        self.assertFalse(tool.annotations.read_only_hint)
                        self.assertEqual(tool.annotations.destructive_hint, destructive)
                        self.assertTrue(tool.annotations.idempotent_hint)
                        self.assertFalse(tool.annotations.open_world_hint)

                    async def call(name, **arguments):
                        return await self.call(session, name, token, **arguments)

                    item = await call("create_inventory_item", key="catalog", **CATALOG)
                    item_id = item["id"]
                    edit = {"item_id": item_id, "revision": item["revision"], **CATALOG,
                            "selling_price_cents": 80}
                    baseline = self.inventory_state()
                    await call("edit_inventory_item", key="partial", error="validation",
                               item_id=item_id, revision=item["revision"], selling_price_cents=80)
                    await call("receive_stock", key="fractional", error="validation",
                               item_id=item_id, revision=item["revision"], quantity_milli=1.5)
                    await call("create_inventory_item", error="validation", **CATALOG)
                    self.assertEqual(self.inventory_state(), baseline)

                    item = await call("edit_inventory_item", key="price", **edit)
                    receipt = await call("receive_stock", key="receipt", item_id=item_id,
                                         revision=item["revision"], quantity_milli=6000)
                    adjustment = await call("adjust_stock", key="adjust", item_id=item_id,
                                            revision=receipt["item"]["revision"], quantity_milli=-500,
                                            reason="Correct receipt count")
                    reversal = await call("reverse_stock_movement", key="reverse", item_id=item_id,
                                          revision=adjustment["item"]["revision"],
                                          movement_id=adjustment["movement"]["id"], reason="Recount agrees")
                    self.assertEqual(reversal["item"]["on_hand_milli"], 6000)
                    self.assertEqual(reversal["movement"]["reverses_id"], adjustment["movement"]["id"])

                    repair = await call("create_repair", key="repair", reported_issue="Synthetic device")
                    repair_id = repair["id"]
                    use = {"repair_id": repair_id, "item_id": item_id, "repair_revision": repair["revision"],
                           "item_revision": reversal["item"]["revision"], "consumed_quantity_milli": 1500,
                           "billed_quantity_milli": 2000, "key": "consume"}
                    charge = await call("consume_part", **use)
                    committed = self.inventory_state()
                    self.assertEqual(await call("consume_part", **use), charge)
                    self.assertEqual(self.inventory_state(), committed)
                    self.assertEqual((charge["consumed_quantity_milli"], charge["quantity_milli"],
                                      charge["unit_cost_cents"], charge["line_total_cents"]), (1500, 2000, 17, 160))
                    for code, changes in (("stale_revision", {"key": "stale"}),
                                          ("conflict", {"billed_quantity_milli": 1000})):
                        error = await call("consume_part", error=code, **(use | changes))
                        self.assertEqual(error["http_status"], 409)
                    self.assertEqual(self.inventory_state(), committed)

                    charge_ref = {"repair_id": repair_id, "charge_id": charge["id"]}
                    review = await call("part_return_status", **charge_ref)
                    returned = await call("return_part", key="physical", **charge_ref, quantity_milli=500,
                                          repair_revision=review["repair_revision"],
                                          item_revision=review["item_revision"], reason="Unused piece")
                    # The return takes 0.5 off the 2.0 billed at the charge price (0.80).
                    self.assertEqual(returned["charge"], charge | {
                        "quantity_milli": 1500, "line_total_cents": 120, "revision": charge["revision"] + 1})
                    self.assertEqual(returned["totals"]["total_cents"], 120)
                    self.assertEqual(returned["remaining_quantity_milli"], 1000)
                    self.assertEqual(returned["stock"]["item"]["on_hand_milli"], 5000)
                    review = await call("part_return_status", **charge_ref)
                    mistake = await call("correct_intake_mistake", key="mistake", **charge_ref,
                                         repair_revision=review["repair_revision"], item_revision=review["item_revision"],
                                         charge_revision=review["charge_revision"], reason="Wrong repair selected")
                    self.assertTrue(mistake["charge"]["deleted"])
                    self.assertEqual(mistake["charge"]["revision"], charge["revision"] + 2)
                    self.assertEqual(mistake["totals"]["total_cents"], 0)
                    self.assertEqual(mistake["remaining_quantity_milli"], 0)
                    self.assertEqual(mistake["stock"]["item"]["on_hand_milli"], 6000)

                    item = await call("archive_inventory_item", key="archive", item_id=item_id,
                                      revision=mistake["stock"]["item"]["revision"])
                    self.assertIsNotNone(item["archived_at"])
                    movements = await call("list_stock_movements", item_id=item_id)
                    self.assertEqual([(m["kind"], m["quantity_milli"], m["on_hand_after_milli"])
                                      for m in reversed(movements["movements"])], [
                        ("receive", 6000, 6000), ("adjust", -500, 5500), ("reversal", 500, 6000),
                        ("consume", -1500, 4500), ("return", 500, 5000), ("return", 1000, 6000),
                    ])
                    self.assertEqual([m["reverses_id"] for m in movements["movements"][:2]],
                                     [charge["movement_id"]] * 2)
                    self.assertEqual(movements, api.get(f"/api/v1/inventory/{item_id}/movements",
                                                        headers={"Authorization": f"Bearer {token}"}).json())
                    restored = await call("restore_inventory_item", key="restore", item_id=item_id,
                                          revision=item["revision"])
                    self.assertEqual((restored["revision"], restored["archived_at"], restored["on_hand_milli"]),
                                     (item["revision"] + 1, None, 6000))
                    self.assertEqual(await call("list_stock_movements", item_id=item_id), movements)
                    return restored, mistake["charge"], use, charge

            item, hidden_charge, use, charge = asyncio.run(workflow())

        operations = ["create", "edit", "receive", "adjust", "reverse", "consume", "return", "intake_mistake", "archive", "restore"]
        self.assertEqual(self.snapshot(
            "SELECT operation, actor_user_id, actor_username, source, token_label FROM audit_events "
            "WHERE operation LIKE 'inventory.%' ORDER BY id"
        ), [(f"inventory.{operation}", 2, "mcp", "mcp", None) for operation in operations])

        committed = self.inventory_state()
        with workspace_client(self.workspace) as (api, _):
            async def restart_readback():
                async with self.session(self.profile(api, token), token) as session:
                    self.assertEqual(await self.call(session, "consume_part", token, **use), charge)
                    self.assertEqual(await self.call(session, "get_inventory_item", token, item_id=item["id"]), item)
                    self.assertEqual(await self.call(session, "restore_inventory_item", token, item_id=item["id"],
                                                     revision=item["revision"] - 1, key="restore"), item)
                    self.assertEqual(await self.call(session, "get_charge", token, repair_id=use["repair_id"],
                                                     charge_id=hidden_charge["id"]), hidden_charge)
                    summary = await self.call(session, "charge_summary", token, repair_id=use["repair_id"])
                    self.assertEqual(summary["total_cents"], 0)
                    reconciliation = await self.call(session, "stock_reconciliation", token)
                    self.assertEqual((reconciliation["mismatches"], reconciliation["issues"]), ([], []))

            asyncio.run(restart_readback())
        self.assertEqual(self.inventory_state(), committed)

    def test_stdio_inventory_write_refusals_have_no_side_effects(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
            db.execute("INSERT INTO inventory_items(id, shop_id, sku, description, unit, archived_at) "
                       "VALUES (10, 2, 'OUT', 'Outside item', 'pcs', 'archived')")

        with workspace_client(self.workspace) as (api, _):
            writer = self.token(api, name="Writer")

            async def refuse():
                async with self.session(self.profile(api, writer), writer) as session:
                    item = await self.call(session, "create_inventory_item", writer, key="catalog", **CATALOG)
                    repair = await self.call(session, "create_repair", writer, key="repair", reported_issue="Local")
                    baseline = self.inventory_state()
                    use = {"repair_id": repair["id"], "item_id": item["id"], "repair_revision": repair["revision"],
                           "item_revision": item["revision"], "consumed_quantity_milli": 1000,
                           "billed_quantity_milli": 1000, "key": "consume"}
                    error = await self.call(session, "consume_part", writer, error="not_found",
                                            **(use | {"item_id": 10}))
                    self.assertEqual(error["http_status"], 404)
                    denied = await self.call(session, "restore_inventory_item", writer, error="not_found",
                                             item_id=10, revision=1, key="restore")
                    self.assertEqual(denied["http_status"], 404)
                    self.assertEqual(self.inventory_state(), baseline)


            asyncio.run(refuse())
