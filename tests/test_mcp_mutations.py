"""Exercise reviewed mutations and explicit replay through real MCP stdio."""

import asyncio
from contextlib import closing
from hashlib import sha256
import secrets
import sqlite3
import unittest
from uuid import uuid4

from test_cli_identity import workspace_client
from test_mcp_reads import MCPFixture
from test_repairs import DEVICE


STOCK_TOOLS = {
    "create_inventory_item": (False, set()),
    "edit_inventory_item": (True, {"revision"}),
    "archive_inventory_item": (True, {"revision"}),
    "restore_inventory_item": (False, {"revision"}),
    "receive_stock": (False, {"revision"}),
    "adjust_stock": (True, {"revision"}),
    "reverse_stock_movement": (True, {"revision"}),
    "consume_part": (True, {"repair_revision", "item_revision"}),
    "return_part": (False, {"repair_revision", "item_revision"}),
    "correct_intake_mistake": (True, {"repair_revision", "item_revision", "charge_revision"}),
}


BILLING_TOOLS = {
    "create_charge": (False, None),
    "correct_charge": (True, "revision"),
    "delete_charge": (True, "revision"),
    "restore_charge": (False, "revision"),
    "set_discount": (True, "revision"),
}


ATTACHMENT_TOOLS = {
    "upload_attachment": False,
    "edit_attachment_caption": True,
    "link_attachment": False,
    "unlink_attachment": True,
    "detach_attachment": True,
    "restore_attachment": False,
    "purge_attachment": True,
}


class MCPMutationTests(MCPFixture, unittest.TestCase):
    def business_state(self):
        return tuple(self.snapshot(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "customers", "repairs", "work_entries", "work_entry_revisions",
            "customer_requests", "repair_requests", "audit_events",
        ))

    def test_stdio_mutation_workflows_replay_and_restart(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api, name="MCP writer")
            profile = self.profile(api, token)
            if True:
                async def replay():
                    async with self.session(profile, token) as session:
                        key = uuid4().hex
                        await self.call(session, "create_customer", token, name="Lost acknowledgement", key=key)
                        self.assertEqual(self.snapshot("SELECT id, revision FROM customers"), [(1, 1)])
                        committed = self.business_state()
                        customer = await self.call(session, "create_customer", token,
                                                   name="Lost acknowledgement", key=key)
                        self.assertEqual((customer["id"], customer["revision"]), (1, 1))
                        self.assertTrue(self.business_state() == committed)
                        return customer, key

                customer, create_key = asyncio.run(replay())
                initial_customer = customer

            async def workflow():
                async with self.session(profile, token) as session:
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools
                             if not tool.annotations.read_only_hint}
                    creates = {"create_customer", "create_repair", "create_work_entry"}
                    destructive = {"edit_customer", "archive_customer", "edit_repair", "archive_repair",
                                   "change_repair_stage", "mark_repair_picked_up", "undo_repair_pickup",
                                   "correct_work_entry", "delete_work_entry"}
                    payment = {"mark_repair_paid", "mark_repair_unpaid"}  # Exercised in test_payment_status.
                    workflow_tools = creates | destructive | {
                        "restore_customer",
                        "restore_repair", "restore_work_entry",
                    }
                    self.assertEqual(set(tools), (workflow_tools | payment | STOCK_TOOLS.keys() | BILLING_TOOLS.keys()
                                                  | ATTACHMENT_TOOLS.keys()))
                    for name in workflow_tools:
                        tool = tools[name]
                        self.assertIn("key", tool.input_schema["required"])
                        self.assertEqual("revision" in tool.input_schema["required"], name not in creates)
                        self.assertFalse(tool.input_schema["additionalProperties"])
                        self.assertEqual(tool.output_schema["type"], "object")
                        self.assertEqual(tool.annotations.destructive_hint, name in destructive)
                        self.assertTrue(tool.annotations.idempotent_hint)
                        self.assertFalse(tool.annotations.open_world_hint)

                    async def mutate(tool_name, **kw):
                        return await self.call(session, tool_name, token, key=uuid4().hex, **kw)

                    customer_id = customer["id"]
                    key = uuid4().hex
                    edit = {"customer_id": customer_id, "revision": 1, "phone": "555-0100", "key": key}
                    edited = await self.call(session, "edit_customer", token, **edit)
                    self.assertEqual((edited["revision"], edited["phone"]), (2, "555-0100"))
                    committed = self.business_state()
                    self.assertEqual(await self.call(session, "edit_customer", token, **edit), edited)
                    for code, change in (
                        ("stale_revision", {"key": uuid4().hex}),
                        ("conflict", {"phone": "555-0101"}),
                    ):
                        error = await self.call(session, "edit_customer", token, error=code, **(edit | change))
                        self.assertEqual(error["http_status"], 409)

                    for invalid in (
                        {"revision": True, "key": uuid4().hex},
                        {"revision": 2, "key": "bad key"},
                        {"revision": 2, "key": uuid4().hex, "phone": "555-0101", "actor_id": 7},
                    ):
                        await self.call(session, "edit_customer", token, error="validation", **(edit | invalid))
                    self.assertTrue(self.business_state() == committed)
                    archived = await mutate("archive_customer", customer_id=customer_id, revision=2)
                    self.assertIsNotNone(archived["archived_at"])
                    restored = await mutate("restore_customer", customer_id=customer_id, revision=3)
                    self.assertEqual((restored["revision"], restored["archived_at"]), (4, None))

                    repair = await mutate("create_repair", customer_id=customer_id, **DEVICE)
                    repair_id = repair["id"]
                    changed = await mutate("edit_repair", repair_id=repair_id, revision=1,
                                           customer_id=customer_id, technician_id=None,
                                           **(DEVICE | {"model": "Reviewed model"}))
                    self.assertEqual((changed["revision"], changed["model"]), (2, "Reviewed model"))
                    for revision, stage, reason in ((2, "in_progress", ""), (3, "repaired", ""),
                                                     (4, "in_progress", "Retest requested")):
                        changed = await mutate("change_repair_stage", repair_id=repair_id, revision=revision,
                                               stage=stage, reason=reason)
                        self.assertEqual((changed["revision"], changed["stage"]), (revision + 1, stage))
                    picked_up = await mutate("mark_repair_picked_up", repair_id=repair_id, revision=5)
                    self.assertIsNotNone(picked_up["picked_up_at"])
                    returned = await mutate("undo_repair_pickup", repair_id=repair_id, revision=6)
                    self.assertIsNone(returned["picked_up_at"])
                    hidden = await mutate("archive_repair", repair_id=repair_id, revision=7)
                    self.assertIsNotNone(hidden["archived_at"])
                    repair = await mutate("restore_repair", repair_id=repair_id, revision=8)
                    self.assertEqual((repair["revision"], repair["archived_at"]), (9, None))

                    entry = await mutate("create_work_entry", repair_id=repair_id, kind="note", body="Initial")
                    entry_ids = {"repair_id": repair_id, "entry_id": entry["id"]}
                    self.assertEqual((entry["revision"], entry["author_id"], entry["author_username"]),
                                     (1, 2, "mcp"))
                    corrected = await mutate("correct_work_entry", **entry_ids, revision=1, kind="test",
                                             body="Corrected", reason="Record completed test")
                    self.assertEqual((corrected["revision"], corrected["body"]), (2, "Corrected"))
                    deleted = await mutate("delete_work_entry", **entry_ids, revision=2, reason="Hide entry")
                    self.assertEqual((deleted["revision"], deleted["deleted"]), (3, True))
                    entry = await mutate("restore_work_entry", **entry_ids, revision=3, reason="Restore entry")
                    self.assertEqual((entry["revision"], entry["deleted"]), (4, False))
                    return restored, repair, entry

            customer, repair, entry = asyncio.run(workflow())

        audit = self.snapshot(
            "SELECT operation, entity_id, actor_user_id, actor_username, source, token_label "
            "FROM audit_events WHERE source='mcp' AND actor_kind='user' ORDER BY id"
        )
        (mcp_id,), = self.snapshot("SELECT id FROM users WHERE username='mcp'")
        self.assertEqual(audit, [
            (operation, entity["id"], mcp_id, "mcp", "mcp", None)
            for entity, operations in (
                (customer, ["customer.create", "customer.update", "customer.archive", "customer.restore"]),
                (repair, ["repair.create", "repair.update", "repair.stage", "repair.stage", "repair.stage",
                          "repair.pickup", "repair.pickup", "repair.archive", "repair.archive"]),
                (entry, ["work_entry.create", "work_entry.correct", "work_entry.delete", "work_entry.restore"]),
            ) for operation in operations
        ])
        committed = self.business_state()
        self.assertTrue(token not in repr(committed), "Credential appeared in persisted business data")
        with workspace_client(self.workspace) as (api, _):
            async def readback():
                async with self.session(self.profile(api, token), token) as session:
                    replayed = await self.call(session, "create_customer", token,
                                               name="Lost acknowledgement", key=create_key)
                    self.assertEqual(replayed, initial_customer)
                    ids = {"customer_id": customer["id"]}
                    self.assertEqual(await self.call(session, "get_customer", token, **ids), customer)
                    history = await self.call(session, "customer_history", token, **ids)
                    self.assertEqual([event["operation"] for event in history["items"]],
                                     [row[0] for row in reversed(audit[:4])])
                    repair_id = repair["id"]
                    self.assertEqual(await self.call(session, "get_repair", token, repair_id=repair_id), repair)
                    entries = await self.call(session, "list_work_entries", token, repair_id=repair_id)
                    self.assertEqual(entries["items"], [entry])
                    history = await self.call(session, "work_entry_history", token,
                                              repair_id=repair_id, entry_id=entry["id"])
                    self.assertEqual([(row["revision"], row["reason"], row["editor_id"]) for row in history],
                                     [(4, "Restore entry", mcp_id), (3, "Hide entry", mcp_id),
                                      (2, "Record completed test", mcp_id), (1, "", mcp_id)])

            asyncio.run(readback())
        self.assertTrue(self.business_state() == committed)

    def test_stdio_refusals_leave_business_state_unchanged(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
            db.execute("INSERT INTO users(id, shop_id, username, role, password_hash) "
                       "SELECT 3, 2, 'foreign', 'admin', password_hash FROM users WHERE id=1")
            db.execute("INSERT INTO customers(id, shop_id, name) VALUES (10, 2, 'Outside customer')")
            db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (10, 2, 'Outside repair')")
            db.execute("INSERT INTO work_entries(id, shop_id, repair_id, author_id, author_username, kind, body) "
                       "VALUES (10, 2, 10, 3, 'foreign', 'note', 'Outside entry')")

        with workspace_client(self.workspace) as (api, _):
            writer = self.token(api, name="Writer")

            async def refuse():
                shared_key = uuid4().hex
                async with self.session(self.profile(api, writer), writer) as session:
                    customer = await self.call(session, "create_customer", writer, name="Local", key=shared_key)
                    repair = await self.call(session, "create_repair", writer, reported_issue="Local repair",
                                             key=uuid4().hex)
                    baseline = self.business_state()
                    for name, code, status, args in (
                        ("archive_customer", "not_found", 404, {"customer_id": 10, "revision": 1}),
                        ("create_repair", "validation", 422, {"customer_id": 10, "reported_issue": "Valid"}),
                        ("restore_repair", "not_found", 404, {"repair_id": 10, "revision": 1}),
                        ("create_work_entry", "not_found", 404, {"repair_id": 10, "kind": "note", "body": "Valid"}),
                        ("delete_work_entry", "not_found", 404,
                         {"repair_id": repair["id"], "entry_id": 10, "revision": 1, "reason": "Valid"}),
                    ):
                        error = await self.call(session, name, writer, error=code, key=uuid4().hex, **args)
                        self.assertEqual(error["http_status"], status)
                    self.assertTrue(self.business_state() == baseline)


            asyncio.run(refuse())
