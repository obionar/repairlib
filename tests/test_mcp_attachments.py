"""Exercise attachment writes through real MCP stdio on the local workspace."""

import asyncio
from base64 import b64decode, b64encode
from contextlib import closing
from hashlib import sha256
import sqlite3
import unittest
from uuid import uuid4

from repairlib.storage.blobs import files_root
from test_cli_identity import workspace_client
from test_mcp_mutations import ATTACHMENT_TOOLS
from test_mcp_reads import API, MCPFixture

ORIGINAL = b"\x00\xffSynthetic attachment\r\n\x1a"
MAX_BYTES = 64 * 1024


def encode(data):
    return b64encode(data).decode("ascii")


class MCPAttachmentTests(MCPFixture, unittest.TestCase):
    def post(self, api, token, path, **body):
        response = api.post(API + path, json=body, headers={
            "Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex,
        })
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def files(self):
        return {path.name: path.read_bytes() for path in files_root(self.workspace).rglob("*") if path.is_file()}

    def state(self):
        return self.files(), tuple(self.snapshot(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "attachments", "attachment_links", "attachment_requests", "audit_events",
        ))

    def test_stdio_binary_upload_replay_lifecycle_and_restart(self):
        with workspace_client(self.workspace) as (api, _):
            token = self.token(api, name="Attachment writer")
            repair_id = self.post(api, token, "/repairs", reported_issue="Synthetic repair")
            entry_id = self.post(api, token, f"/repairs/{repair_id}/entries", kind="note", body="Initial")
            path = f"{API}/repairs/{repair_id}/attachments"
            upload = dict(repair_id=repair_id, filename="Μέτρηση +.bin", entry_id=entry_id,
                          content_base64=encode(ORIGINAL), key=uuid4().hex)

            if True:
                profile = self.profile(api, token)

                async def replay_upload():
                    async with self.session(profile, token) as session:
                        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                        self.assertEqual(set(ATTACHMENT_TOOLS) - tools.keys(), set())
                        for name, destructive in ATTACHMENT_TOOLS.items():
                            tool = tools[name]
                            required = {"repair_id", "key"}
                            if name == "upload_attachment":
                                required.update(("filename", "content_base64"))
                            else:
                                required.update(("attachment_id", "revision"))
                            if name in {"link_attachment", "unlink_attachment"}:
                                required.add("entry_id")
                            if name == "edit_attachment_caption":
                                required.add("caption")
                            self.assertEqual(set(tool.input_schema["required"]), required)
                            self.assertFalse(tool.input_schema["additionalProperties"])
                            self.assertEqual(tool.output_schema["type"], "object")
                            self.assertFalse(tool.annotations.read_only_hint)
                            self.assertEqual(tool.annotations.destructive_hint, destructive)
                            self.assertTrue(tool.annotations.idempotent_hint)
                            self.assertFalse(tool.annotations.open_world_hint)
                        schema = tools["upload_attachment"].input_schema["properties"]
                        self.assertEqual(schema["content_base64"]["maxLength"], 87384)
                        attachment = await self.call(session, "upload_attachment", token, **upload)
                        self.assertEqual(list(self.files().values()), [ORIGINAL])
                        saved = self.state()
                        self.assertEqual(await self.call(session, "upload_attachment", token, **upload), attachment)
                        self.assertTrue(self.state() == saved)
                        return attachment

                attachment = asyncio.run(replay_upload())

            profile = self.profile(api, token)
            refs = dict(repair_id=repair_id, attachment_id=attachment["id"])
            self.assertEqual((attachment["filename"], attachment["size_bytes"], attachment["sha256"],
                              attachment["revision"], attachment["entry_ids"]),
                             (upload["filename"], len(ORIGINAL), sha256(ORIGINAL).hexdigest(), 1, [entry_id]))
            auth = {"Authorization": f"Bearer {token}"}
            self.assertEqual(api.get(f"{path}/{attachment['id']}/content", headers=auth).content, ORIGINAL)

            async def lifecycle():
                async with self.session(profile, token) as session:
                    content = await self.call(session, "read_attachment_content", token, **refs)
                    self.assertEqual(content["attachment"], attachment)
                    self.assertEqual(b64decode(content["content_base64"], validate=True), ORIGINAL)
                    baseline = self.state()
                    error = await self.call(session, "upload_attachment", token, error="conflict",
                                            **(upload | {"content_base64": encode(ORIGINAL + b"!")}))
                    self.assertEqual(error["http_status"], 409)
                    self.assertTrue(self.state() == baseline)
                    caption = refs | dict(revision=1, caption="Reviewed binary", key=uuid4().hex)
                    edited = await self.call(session, "edit_attachment_caption", token, **caption)
                    self.assertEqual((edited["caption"], edited["revision"]), (caption["caption"], 2))
                    baseline = self.state()
                    self.assertEqual(await self.call(session, "edit_attachment_caption", token, **caption), edited)
                    for code, change in (("stale_revision", {"key": uuid4().hex}),
                                         ("conflict", {"caption": "Changed intent"})):
                        error = await self.call(session, "edit_attachment_caption", token, error=code,
                                                **(caption | change))
                        self.assertEqual(error["http_status"], 409)
                    self.assertTrue(self.state() == baseline)
                    unlinked = await self.call(session, "unlink_attachment", token, **refs, entry_id=entry_id,
                                               revision=2, key=uuid4().hex)
                    self.assertEqual((unlinked["entry_ids"], unlinked["revision"]), ([], 3))
                    linked = await self.call(session, "link_attachment", token, **refs, entry_id=entry_id,
                                             revision=3, key=uuid4().hex)
                    self.assertEqual((linked["entry_ids"], linked["revision"]), ([entry_id], 4))
                    detached = await self.call(session, "detach_attachment", token, **refs,
                                               revision=4, key=uuid4().hex)
                    self.assertTrue(detached["detached"])
                    restored = await self.call(session, "restore_attachment", token, **refs,
                                               revision=5, key=uuid4().hex)
                    self.assertEqual((restored["detached"], restored["revision"], restored["entry_ids"],
                                      restored["sha256"]), (False, 6, [entry_id], attachment["sha256"]))
                    return restored

            restored = asyncio.run(lifecycle())

        self.assertEqual(self.snapshot(
            "SELECT operation, actor_username, source, token_label FROM audit_events "
            "WHERE entity_type='attachment' ORDER BY id"
        ), [(f"attachment.{op}", "mcp", "mcp", None) for op in (
            "create", "caption", "unlink", "link", "detach", "restore",
        )])
        baseline = self.state()
        with workspace_client(self.workspace) as (api, _):
            async def readback():
                async with self.session(self.profile(api, token), token) as session:
                    self.assertEqual(await self.call(session, "upload_attachment", token, **upload), attachment)
                    content = await self.call(session, "read_attachment_content", token, **refs)
                    self.assertEqual(content["attachment"], restored)
                    self.assertEqual(b64decode(content["content_base64"], validate=True), ORIGINAL)

            asyncio.run(readback())
        self.assertTrue(self.state() == baseline)

    def test_stdio_upload_bounds_and_purge_retention(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside shop')")
            db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (50, 2, 'Outside repair')")

        with workspace_client(self.workspace) as (api, _):
            owner = self.token(api)
            repair = self.post(api, owner, "/repairs", reported_issue="Synthetic file repair")
            other = self.post(api, owner, "/repairs", reported_issue="Other local repair")
            other_entry = self.post(api, owner, f"/repairs/{other}/entries", kind="note", body="Other entry")
            cap_bytes = b"z" * MAX_BYTES
            upload = dict(repair_id=repair, filename="bounds.bin", content_base64=encode(cap_bytes), key=uuid4().hex)
            marker = "Base64PayloadMustStayOutOfErrors"
            oversized = encode(b"z" * (MAX_BYTES + 4))
            sensitive = marker, oversized[:128]

            async def verify():
                profile = self.profile(api, owner)
                async with self.session(profile, owner, sensitive=sensitive) as session:
                    baseline = self.state()
                    invalid = (
                        {"content_base64": ""},
                        {"content_base64": marker + "!"},
                        {"content_base64": "YQ==\n"},
                        {"content_base64": "_w=="},
                        {"content_base64": "YR=="},  # Nonzero unused padding bits
                        {"content_base64": "YQ"},
                        {"content_base64": encode(b"z" * (MAX_BYTES + 1))},  # Same encoded length as the cap
                        {"content_base64": oversized},
                        {"path": "/unused/local-file"},
                    )
                    for change in invalid:
                        reply = await session.call_tool("upload_attachment", upload | change)
                        rendered = reply.model_dump_json()
                        self.assertTrue(all(value not in rendered for value in (owner, *sensitive)),
                                        "Sensitive argument appeared in tool output")
                        self.assertTrue(reply.is_error)
                        self.assertEqual(reply.structured_content["error"]["code"], "validation")
                    self.assertTrue(self.state() == baseline)
                    for code, status, change in (
                        ("not_found", 404, {"repair_id": 50}),
                        ("validation", 422, {"entry_id": other_entry}),
                    ):
                        error = await self.call(session, "upload_attachment", owner, error=code, **(upload | change))
                        self.assertEqual(error["http_status"], status)
                    self.assertTrue(self.state() == baseline)
                    attachment = await self.call(session, "upload_attachment", owner, **upload)
                    self.assertEqual((attachment["size_bytes"], attachment["sha256"]),
                                     (MAX_BYTES, sha256(cap_bytes).hexdigest()))
                    self.assertEqual(list(self.files().values()), [cap_bytes])
                    refs = dict(repair_id=repair, attachment_id=attachment["id"])
                    review = refs | dict(revision=1, key=uuid4().hex)
                    baseline = self.state()
                    for tool, code, status, arguments in (
                        ("edit_attachment_caption", "not_found", 404, review | dict(repair_id=other, caption="Wrong")),
                        ("link_attachment", "validation", 422, review | dict(entry_id=other_entry)),
                        ("purge_attachment", "retention", 409, review),
                    ):
                        error = await self.call(session, tool, owner, error=code, **arguments)
                        self.assertEqual(error["http_status"], status)
                    self.assertTrue(self.state() == baseline)
                    await self.call(session, "detach_attachment", owner, **review)
                    purge = refs | dict(revision=2, key=uuid4().hex)

                    def age(days):
                        with closing(sqlite3.connect(self.db)) as db, db:
                            db.execute("UPDATE attachments SET detached_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                                       "WHERE id=?", (f"-{days} days", attachment["id"]))

                    age(29)
                    baseline = self.state()
                    early = await self.call(session, "purge_attachment", owner, error="retention", **purge)
                    self.assertEqual(early["http_status"], 409)
                    self.assertTrue(self.state() == baseline)
                    age(31)
                    baseline = self.state()
                    purged = await self.call(session, "purge_attachment", owner, **purge)
                    self.assertEqual((purged["purged"], purged["revision"], purged["filename"], purged["sha256"]),
                                     (True, 3, attachment["filename"], attachment["sha256"]))
                    baseline = self.state()
                    self.assertEqual(await self.call(session, "purge_attachment", owner, **purge), purged)
                    error = await self.call(session, "read_attachment_content", owner, error="purged", **refs)
                    self.assertEqual(error["http_status"], 410)
                    await self.call(session, "restore_attachment", owner, error="purged", **refs,
                                    revision=3, key=uuid4().hex)
                    self.assertTrue(self.state() == baseline)
                    self.assertEqual(self.files(), {})

            asyncio.run(verify())
