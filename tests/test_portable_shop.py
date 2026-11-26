"""S5 portable shop gate through the authenticated archive API and CLI."""

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import secrets
import sqlite3
import sys
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.config import DISK_RESERVE_BYTES
from repairlib.contracts import PortablePreview
from repairlib.domain import portable_shop
from repairlib.domain.users import ensure_local_actor
from repairlib.domain.setup import initialize_workspace
from repairlib.storage.blobs import StagedBlob
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_inventory_adapters import ITEM
from test_repairs import DEVICE
from test_tokens import ACCOUNT, ORIGIN, PASSWORD, TokenFixture, issued_secret

API = "/api/v1"
EXPORT = API + "/portable/export"
PREVIEW = API + "/portable/preview"
IMPORT = API + "/portable/import"
ORIGINAL = b"synthetic original\x00\xff"
DETACHED = b"synthetic detached\x00\xfe"
PURGED = b"synthetic purged\x00\xfd"


def file_record(content):
    return {"size": len(content), "sha256": sha256(content).hexdigest()}


def pack(files, *, extra=None):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            entry = ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, payload)
        if extra:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(*extra)
    return buffer.getvalue()


class PortableShopTests(TokenFixture, unittest.TestCase):
    def token(self, api, **options):
        if not api.cookies:
            self.sign_in(api)
        return issued_secret(api.post(ACCOUNT, data=self.issue_form(api, **options)))

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    def mutate(self, api, token, path, *, status=201, method="POST", key=None, **kwargs):
        reply = api.request(method, API + path, **kwargs,
                            headers={**self.auth(token), "Idempotency-Key": key or uuid4().hex})
        self.assertEqual(reply.status_code, status, reply.text)
        return reply.json()

    def state(self, workspace=None):
        workspace = workspace or self.workspace
        with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
            return list(db.iterdump()), {
                path.relative_to(workspace): path.read_bytes()
                for path in (workspace / "state" / "files").rglob("*") if path.is_file()
            }

    def test_schema21_export_cli_preview_and_shop_boundary(self):
        with workspace_client(self.workspace) as (api, _):
            owner = self.token(api, name="Synthetic private credential label")
            shop_reader = self.token(api, scope="read")
            technician = "rl_" + secrets.token_urlsafe(32)
            other = "rl_" + secrets.token_urlsafe(32)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops VALUES (2, 'Outside shop')")
                db.execute("INSERT INTO customers(shop_id, name) VALUES (2, 'Outside customer')")
                for shop, role, token in ((1, "technician", technician), (2, "admin", other)):
                    user = db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                                      "VALUES (?, ?, ?, 'synthetic hash')",
                                      (shop, "fixture-" + role, role)).lastrowid
                    if role == "technician":
                        technician_id = user
                    db.execute("INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, "
                               "name, scope, create_key) VALUES (?, ?, 1, ?, 'fixture', 'write', ?)",
                               (shop, user, sha256(token.encode()).hexdigest(), uuid4().hex))

            def post(path, **kwargs):
                return self.mutate(api, owner, path, **kwargs)

            customer = post("/customers", json={"name": "Portable customer"}, key="source-customer-key")
            repair = post("/repairs", json={**DEVICE, "customer_id": customer["id"], "technician_id": technician_id})
            base = f"/repairs/{repair['id']}"
            entry = self.mutate(api, technician, base + "/entries",
                                json={"kind": "note", "body": "Attributed work"})
            item = post("/inventory", json=ITEM)
            post(f"/inventory/{item['id']}/receive", json={"quantity_milli": 5000, "revision": 1})
            charge = post(base + "/consume-part", json={
                "item_id": item["id"], "repair_revision": repair["revision"], "item_revision": 2,
                "consumed_quantity_milli": 1000, "billed_quantity_milli": 1500, "unit_price_cents": 1999,
            })
            post(base + f"/charges/{charge['id']}/return-part", json={
                "repair_revision": repair["revision"], "item_revision": 3,
                "quantity_milli": 250, "reason": "Unused portion",
            })
            post(base + "/discount", method="PUT", status=200,
                 json={"kind": "percent", "value": 10, "revision": 0})
            post("/settings", method="PATCH", status=200,
                 json={"revision": 1, "timezone": "Europe/Nicosia", "inline_preview_mib": 4})
            uploads = [post(base + "/attachments", params={"filename": "a.bin", "entry_id": entry["id"]},
                            content=blob) for blob in (ORIGINAL, DETACHED, PURGED)]
            for attachment in uploads[1:]:
                post(base + f"/attachments/{attachment['id']}/detach", status=200,
                     json={"revision": attachment["revision"], "detached": True})
            purged = uploads[-1]["id"]
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE attachments SET detached_at='2000-01-01T00:00:00.000Z' WHERE id=?",
                           (purged,))
            post(base + f"/attachments/{purged}/purge", status=200, json={"revision": 2})
            ensure_local_actor(self.workspace, "cli")  # First CLI use writes this once.
            before = self.state()

            for token, code in ((None, 401), (technician, 403), (other, 401)):
                headers = self.auth(token) if token else {}
                self.assertEqual(api.get(EXPORT, headers=headers).status_code, code)
                self.assertEqual(api.post(PREVIEW, headers=headers, content=b"invalid").status_code, code)
            self.assertEqual(api.get(EXPORT, params={"shop_id": 2},
                                     headers=self.auth(owner)).status_code, 422)
            output = self.workspace.parent / "portable.zip"

            def cli(action, archive=output):
                result = subprocess.run([*COMMAND, "portable", action, str(archive), "--workspace",
                                         str(self.workspace), "--json"], env=python_env(), text=True,
                                        capture_output=True, timeout=15)
                self.assertTrue(shop_reader not in result.stdout + result.stderr)
                return result

            exported = cli("export")
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            original_zip = output.read_bytes()
            self.assertNotEqual(cli("export").returncode, 0)
            self.assertEqual(output.read_bytes(), original_zip)
            previewed = cli("preview")
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            preview = json.loads(previewed.stdout)
            self.assertEqual(self.state(), before)
            storage_name, = self.snapshot("SELECT storage_name FROM attachments WHERE id=?",
                                          (uploads[0]["id"],))[0]
            blob_path = self.workspace / "state" / "files" / "shop-1" / storage_name
            blob_path.write_bytes(b"x" * len(ORIGINAL))
            try:
                broken = api.get(EXPORT, headers=self.auth(owner))
                self.assertEqual(broken.status_code, 409)
                self.assertEqual(broken.json()["error"]["code"], "export_unavailable")
                self.assertNotIn(ORIGINAL, broken.content)
                failed_output = output.with_name("failed.zip")
                self.assertEqual(cli("export", failed_output).returncode, 7)
                self.assertFalse(failed_output.exists())
                self.assertEqual(list(output.parent.glob(".repairlib-portable-*")), [])
            finally:
                blob_path.write_bytes(ORIGINAL)
            self.assertEqual(self.state(), before)

        with ZipFile(output) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            records = json.loads(archive.read("records.json"))
            self.assertEqual((manifest["format"], manifest["schema"]), ("repairlib-portable-shop-2", 43))
            self.assertEqual(manifest["source_shop"], {"id": 1, "name": "Synthetic"})
            self.assertEqual(preview, {k: manifest[k] for k in ("format", "schema", "source_shop", "counts")}
                             | {"attachment_files": 2, "attachment_bytes": len(ORIGINAL) + len(DETACHED),
                                "omitted": list(PortablePreview.omitted)})
            self.assertEqual(manifest["counts"], {name: len(rows) for name, rows in records.items()})
            self.assertEqual(manifest["files"], {
                name: file_record(archive.read(name)) for name in archive.namelist() if name != "manifest.json"
            })
            self.assertEqual(archive.read(f"attachments/{uploads[0]['id']}"), ORIGINAL)
            self.assertEqual(archive.read(f"attachments/{uploads[1]['id']}"), DETACHED)
            self.assertNotIn(f"attachments/{purged}", archive.namelist())
            self.assertIsNotNone(records["attachments"][-1]["purged_at"])
            self.assertEqual(records["attachment_links"][0]["entry_id"], entry["id"])
            self.assertEqual(records["repairs"][0]["customer_id"], customer["id"])
            self.assertEqual(records["work_entry_revisions"][0]["editor_username"], "fixture-technician")
            self.assertEqual([row["kind"] for row in records["inventory_movements"]], ["receive", "consume", "return"])
            self.assertEqual(records["repair_part_consumptions"][0]["charge_id"], charge["id"])
            self.assertNotIn("estimates", records)  # Retired at schema 36; format 1 kept empty keys.
            self.assertEqual(records["shop_settings"][0]["timezone"], "Europe/Nicosia")
            self.assertTrue(any(row["actor_username"] == "fixture-technician" for row in records["audit_events"]))
            forbidden = ("password_hash", "token_hash", "token_label", "api_token", "sessions",
                         "Outside customer", "Outside shop", "Synthetic private credential label",
                         PASSWORD, owner, shop_reader, technician, other)
            self.assertTrue(all(value.encode() not in original_zip for value in forbidden))
            # The built-in actors' '!' marker is not a secret; real password hashes are.
            for query in ("SELECT password_hash FROM users WHERE password_hash <> '!'",
                          "SELECT token_hash FROM sessions",
                          "SELECT token_hash FROM api_tokens"):
                for (value,) in self.snapshot(query):
                    self.assertNotIn(value.encode(), original_zip)
        with workspace_client(self.workspace) as (api, _):
            reply = api.post(PREVIEW, headers=self.auth(shop_reader), content=original_zip)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json(), preview)
            refused = api.post(IMPORT, params={"destination_shop_id": 1, "revision": 2},
                               headers=self.auth(owner) | {"Idempotency-Key": "source-occupied"},
                               content=original_zip)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "destination_occupied")
            self.assertEqual(self.state(), before)

        self.assert_import_roundtrip(output, original_zip, records, owner)

    def test_preview_rejects_malformed_archives_without_business_writes(self):
        with self.client() as api:
            token = self.token(api)
            repair = self.mutate(api, token, "/repairs", json=DEVICE)
            attachment = self.mutate(api, token, f"/repairs/{repair['id']}/attachments",
                                     params={"filename": "sample.bin"}, content=ORIGINAL)
            member = f"attachments/{attachment['id']}"
            headers = self.auth(token)
            reply = api.get(EXPORT, headers=headers)
            self.assertEqual(reply.status_code, 200, reply.text)
            with ZipFile(BytesIO(reply.content)) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            manifest = json.loads(files["manifest.json"])
            records = json.loads(files["records.json"])
            before = self.state()

            def change(*, field=None, value=None, mutate_records=None):
                data = files.copy()
                metadata = deepcopy(manifest)
                if field is not None:
                    metadata[field] = value
                if mutate_records:
                    changed = deepcopy(records)
                    mutate_records(changed)
                    data["records.json"] = json.dumps(changed).encode()
                    metadata["files"]["records.json"] = file_record(data["records.json"])
                data["manifest.json"] = json.dumps(metadata).encode()
                return pack(data)

            link = ZipInfo("attachments/2")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            compressed = ZipInfo("attachments/2")
            compressed.create_system = 3
            compressed.external_attr = (stat.S_IFREG | 0o600) << 16
            compressed.compress_type = ZIP_DEFLATED
            for reason, payload in {
                "traversal": pack(files | {"../escape": b"x"}),
                "duplicate": pack(files, extra=("records.json", b"x")),
                "symlink": pack(files, extra=(link, b"x")),
                "compression": pack(files, extra=(compressed, b"x")),
                "unexpected member": pack(files | {"attachments/2": b"x"}),
                "unexpected tail": reply.content + b"tail",
                "records hash": change(field="files", value=manifest["files"] | {
                    "records.json": file_record(b"x" * len(files["records.json"])),
                }),
                "attachment hash": pack(files | {member: b"x" * len(ORIGINAL)}),
                "size": change(field="files", value=manifest["files"] | {"records.json": file_record(b"")}),
                "count": change(field="counts", value=manifest["counts"] | {"repairs": 0}),
                "schema": change(field="schema", value=22),
                "identity": change(field="source_shop", value={"id": True, "name": "Synthetic"}),
                "identity name": change(field="source_shop", value={"id": 1, "name": "bad\nname"}),
                "dangling reference": change(mutate_records=lambda data: data["repairs"][0].update(customer_id=42)),
                "foreign row": change(mutate_records=lambda data: data["repairs"][0].update(shop_id=2)),
                "wrong column": change(mutate_records=lambda data: data["users"][0].update(password_hash="bad")),
                "wrong type": change(mutate_records=lambda data: data["repairs"][0].update(revision="1")),
            }.items():
                with self.subTest(reason=reason):
                    refused = api.post(PREVIEW, headers=headers, content=payload)
                    self.assertEqual(refused.status_code, 422, refused.text)
                    self.assertEqual(refused.json()["error"]["code"], "invalid_archive")
            oversized = api.post(PREVIEW, headers={**headers, "Content-Length": str((1 << 30) + 1)})
            self.assertEqual(oversized.status_code, 413)
            outside = deepcopy(records)
            for rows in outside.values():
                for row in rows:
                    row["shop_id"] = 2
            foreign = change(field="source_shop", value={"id": 2, "name": "Another synthetic shop"},
                             mutate_records=lambda data: data.update(outside))
            foreign_preview = api.post(PREVIEW, headers=headers, content=foreign)
            self.assertEqual(foreign_preview.status_code, 200, foreign_preview.text)
            self.assertEqual(foreign_preview.json()["source_shop"]["id"], 2)
            self.assertEqual(self.state(), before)

    def destination(self):
        workspace = self.workspace.parent / "destination"
        initialize_workspace(workspace, shop="Destination", timezone="UTC",
                             username="owner", password=PASSWORD)
        return workspace

    def assert_import_roundtrip(self, archive, payload, source_records, source_token):
        dest = self.destination()
        database = dest / "state" / "repairlib.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            # Keep occupied shop 1 in the same DB to force distinct repair/catalog IDs.
            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Outside destination')")
            db.execute("INSERT INTO repairs(shop_id, reported_issue) VALUES (1, 'Outside repair')")
            db.execute("INSERT INTO work_entries(shop_id, repair_id, author_id, author_username, kind, body) "
                       "VALUES (1, 1, 1, 'owner', 'work', 'Outside entry')")
            db.execute("INSERT INTO inventory_items(shop_id, sku, description, unit) "
                       "VALUES (1, 'OUTSIDE', 'Outside stock', 'pcs')")
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Explicit destination')")
            db.execute("INSERT INTO shop_settings(shop_id, timezone) VALUES (2, 'UTC')")
            db.execute("INSERT INTO users(shop_id, username, password_hash, role) "
                       "SELECT 2, username, password_hash, role FROM users WHERE shop_id=1")
            db.execute("UPDATE local_workspace SET shop_id=2")
            admin = db.execute("SELECT * FROM users WHERE shop_id=2").fetchone()

        params = {"destination_shop_id": 2, "revision": 1}
        with workspace_client(dest) as (api, _):
            token = self.token(api)
            reader = self.token(api, scope="read")
            before = self.state(dest)
            write_headers = self.auth(token) | {"Idempotency-Key": "round-trip"}
            for credential, query, code in (
                (token, params | {"destination_shop_id": 1}, 403),
                (source_token, params, 401),
                (reader, params, 403),
                (token, params | {"revision": 2}, 409),
            ):
                denied = api.post(IMPORT, params=query, content=payload,
                                  headers=self.auth(credential) | {"Idempotency-Key": "round-trip"})
                self.assertEqual(denied.status_code, code, denied.text)
            with ZipFile(BytesIO(payload)) as original:
                files = {name: original.read(name) for name in original.namelist()}
            invalid_records = deepcopy(source_records)
            event = next(row for row in invalid_records["audit_events"] if row["operation"] == "inventory.return")
            event["changes"] = json.dumps(json.loads(event["changes"]) | {"reverses_id": 1})
            manifest = json.loads(files["manifest.json"])
            files["records.json"] = json.dumps(invalid_records).encode()
            manifest["files"]["records.json"] = file_record(files["records.json"])
            files["manifest.json"] = json.dumps(manifest).encode()
            refused = api.post(IMPORT, params=params, headers=write_headers, content=pack(files))
            self.assertEqual(refused.status_code, 422, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "invalid_archive")
            self.assertEqual(self.state(dest), before)

            import_command = [
                *COMMAND, "portable", "import", str(archive), "--workspace", str(dest), "--json",
                "--destination-shop-id", "2", "--revision", "1", "--key", "round-trip",
            ]
            imported = subprocess.run(import_command, env=python_env(), text=True, capture_output=True, timeout=20)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            receipt = json.loads(imported.stdout)
            self.assertEqual(receipt["destination_shop_id"], 2)
            self.assertEqual(receipt["source_shop"], {"id": 1, "name": "Synthetic"})
            self.assertEqual(receipt["attachment_files"], 2)
            self.assertEqual(receipt["counts"], {table: len(rows) for table, rows in source_records.items()})

        with workspace_client(dest) as (api, _):
            self.sign_in(api)
            after = self.state(dest)
            # Keys belong to the actor that sent them, so the CLI replays its own import.
            retry = subprocess.run(import_command, env=python_env(), text=True, capture_output=True, timeout=20)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertEqual(json.loads(retry.stdout), receipt)
            changed = [*COMMAND, "portable", "import", str(archive), "--workspace", str(dest), "--json",
                       "--destination-shop-id", "2", "--revision", "2", "--key", "round-trip"]
            conflict = subprocess.run(changed, env=python_env(), text=True, capture_output=True, timeout=20)
            self.assertEqual(conflict.returncode, 7, conflict.stderr)
            occupied = api.post(IMPORT, params=params, content=payload,
                                headers=write_headers | {"Idempotency-Key": "fresh-key"})
            self.assertEqual(occupied.json()["error"]["code"], "destination_occupied")
            self.assertEqual(self.state(dest), after)

            def get(path, status=200):
                response = api.get(API + path, headers=self.auth(token))
                self.assertEqual(response.status_code, status, response.text if status != 200 else "")
                return response

            repair = get("/repairs/2").json()
            # User 4 is the destination's `cli` actor, created by the CLI import before it ran.
            self.assertEqual((repair["customer_id"], repair["technician_id"]), (2, 5))
            get("/repairs/1", 404)
            self.assertEqual(get("/customers/2").json()["name"], "Portable customer")
            self.assertEqual(get("/inventory/reconciliation").json(),
                             {"items_checked": 1, "mismatches": [], "issues": []})
            self.assertEqual(get("/inventory/2").json()["on_hand_milli"], 4250)
            summary = get("/repairs/2/charges?include_consumption=true").json()
            self.assertEqual(summary["total_cents"], 2249)  # 1.25 billed after the 0.25 return, less 10%
            self.assertEqual(summary["items"][0]["inventory_item_id"], 2)
            settings = get("/settings").json()
            self.assertEqual((settings["name"], settings["timezone"], settings["revision"]),
                             ("Explicit destination", "Europe/Nicosia", 2))
            copied = get("/portable/export").content
            with ZipFile(BytesIO(copied)) as archive_file:
                rows = json.loads(archive_file.read("records.json"))
                self.assertEqual(rows["work_entries"][0]["author_username"], "fixture-technician")
                self.assertEqual(rows["work_entry_revisions"][0]["editor_id"], 5)
                self.assertEqual(rows["attachment_links"][0]["repair_id"], 2)
                self.assertEqual(rows["inventory_movements"][-1]["reverses_id"], 2)
                consume = next(row for row in rows["audit_events"] if row["operation"] == "inventory.consume")
                work_entry_id = json.loads(consume["changes"])["work_entry_id"]
                self.assertEqual(work_entry_id, rows["work_entries"][-1]["id"])
                self.assertEqual(rows["work_entries"][-1]["kind"], "work")
                for row, original in zip(rows["attachments"], (ORIGINAL, DETACHED, PURGED)):
                    self.assertEqual(row["sha256"], sha256(original).hexdigest())
                    self.assertEqual(row["uploader_username"], "owner")
                    self.assertEqual(row["uploader_id"], 4)
                    if not row["purged_at"]:
                        self.assertEqual(archive_file.read(f"attachments/{row['id']}"), original)
                self.assertEqual([bool(row["detached_at"]) for row in rows["attachments"]], [False, True, True])
                self.assertEqual(rows["attachments"][-1]["purged_by_username"], "owner")
            self.assertEqual(get("/repairs/2/attachments/1/content").content, ORIGINAL)
            get("/repairs/2/attachments/3/content", 410)

            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("SELECT * FROM users WHERE id=2").fetchone(), admin)
                self.assertEqual(db.execute("""
                    SELECT id, username, active, password_hash FROM users WHERE shop_id=2 AND id<>2 ORDER BY id
                """).fetchall(), [(3, "cli", 1, "!"), (4, "imported-4", 0, ""),
                                  (5, "fixture-technician", 0, ""), (6, "imported-6", 0, "")])
                self.assertEqual(db.execute("SELECT DISTINCT user_id FROM api_tokens").fetchall(), [(2,)])
                self.assertEqual(db.execute("SELECT DISTINCT user_id FROM sessions").fetchall(), [(2,)])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM stock_requests").fetchone(), (0,))
                self.assertEqual(db.execute("""
                    SELECT actor_user_id, actor_username FROM audit_events WHERE operation='portable.import'
                """).fetchall(), [(3, "cli")])
                self.assertEqual(db.execute("""
                    SELECT actor_user_id, actor_username, json_extract(changes,'$.item_id')
                    FROM audit_events WHERE operation='inventory.return'
                """).fetchall(), [(4, "owner", 2)])
            customer = self.mutate(api, token, "/customers", key="source-customer-key",
                                   json={"name": "Portable customer"})
            self.assertEqual(customer["id"], 3)
            self.mutate(api, token, "/inventory/2/receive", json={"quantity_milli": 1000, "revision": 4})
            self.assertEqual(get("/inventory/reconciliation").json()["issues"], [])

    def test_older_archive_with_estimates_imports_without_them(self):
        # The format-1 fixture is a real older archive: one repair (ID 1) with two labour charges.
        with ZipFile(Path(__file__).parent / "fixtures" / "portable" / "format1-paid-ticket.zip") as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        repair = {"id": 1}
        manifest, records = json.loads(files["manifest.json"]), json.loads(files["records.json"])
        owner = records["users"][0]
        shop_id = records["repairs"][0]["shop_id"]
        records["estimates"] = [{
            "id": 1, "shop_id": shop_id, "repair_id": repair["id"], "version": 1, "status": "draft",
            "note": "Old quote", "parts_subtotal_cents": 0, "labor_subtotal_cents": 5000,
            "subtotal_cents": 5000, "discount_kind": "none", "discount_value": 0, "discount_cents": 0,
            "total_cents": 5000, "created_by": owner["id"], "created_by_username": owner["username"],
            "created_at": "2026-11-01T10:00:00.000Z", "accepted_by": None, "accepted_by_username": None,
            "accepted_at": None}]
        records["estimate_lines"] = [{
            "id": 1, "shop_id": shop_id, "estimate_id": 1, "position": 1, "kind": "labor",
            "description": "Bench labour", "quantity_milli": 1000, "unit": "h", "unit_price_cents": 5000,
            "line_total_cents": 5000}]
        template = next(row for row in records["audit_events"] if row["actor_kind"] == "user")
        records["audit_events"].append(template | {
            "id": max(row["id"] for row in records["audit_events"]) + 1, "operation": "estimate.create",
            "entity_type": "estimate", "entity_id": 1, "changes": None})
        for table in ("estimates", "estimate_lines", "audit_events"):
            manifest["counts"][table] = len(records[table])
        files["records.json"] = json.dumps(records).encode()
        manifest["files"]["records.json"] = file_record(files["records.json"])
        files["manifest.json"] = json.dumps(manifest).encode()
        older = pack(files)

        dest = self.destination()
        with TestClient(create_app(workspace=dest), base_url=ORIGIN, follow_redirects=False) as api:
            token = self.token(api)
            previewed = api.post(PREVIEW, headers=self.auth(token), content=older)
            self.assertEqual(previewed.status_code, 200, previewed.text)
            imported = api.post(IMPORT, params={"destination_shop_id": 1, "revision": 1}, content=older,
                                headers=self.auth(token) | {"Idempotency-Key": "older-archive"})
            self.assertEqual(imported.status_code, 201, imported.text)
        with closing(sqlite3.connect(dest / "state" / "repairlib.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'estimate%'").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE entity_type = 'estimate'"
                                        " OR operation LIKE 'estimate.%'").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT description FROM repair_charges ORDER BY id").fetchall(),
                             [("Diagnosis",), ("Labour",)])

    def test_import_refuses_low_disk_before_writes_but_allows_replay(self):
        with self.client() as source:
            token = self.token(source)
            repair = self.mutate(source, token, "/repairs", json=DEVICE)
            self.mutate(source, token, f"/repairs/{repair['id']}/attachments",
                        params={"filename": "sample.bin"}, content=ORIGINAL)
            exported = source.get(EXPORT, headers=self.auth(token))
            self.assertEqual(exported.status_code, 200, exported.text)

        dest = self.destination()
        with TestClient(create_app(workspace=dest), base_url=ORIGIN, follow_redirects=False) as api:
            token = self.token(api)
            reader = self.token(api, scope="read")
            headers = self.auth(token) | {"Idempotency-Key": "disk-reserve"}
            params = {"destination_shop_id": 1, "revision": 1}
            before = self.state(dest)
            free = DISK_RESERVE_BYTES + len(exported.content) - 1
            with (patch("shutil.disk_usage", return_value=SimpleNamespace(free=free)) as usage,
                  patch.object(portable_shop.storage, "insert_import", wraps=portable_shop.storage.insert_import) as insert,
                  patch.object(portable_shop.blobs, "StagedBlob", wraps=StagedBlob) as stage):
                refused = api.post(IMPORT, params=params, headers=headers, content=exported.content)
                self.assertEqual(refused.status_code, 507, refused.text)
                self.assertEqual(refused.json()["error"]["code"], "insufficient_storage")
                usage.assert_called_once_with(dest / "state")
                insert.assert_not_called()
                stage.assert_not_called()
            self.assertEqual(self.state(dest), before)  # Includes audit rows and the import receipt.

            with patch("shutil.disk_usage", return_value=SimpleNamespace(free=free + 1)):
                completed = api.post(IMPORT, params=params, headers=headers, content=exported.content)
                self.assertEqual(completed.status_code, 201, completed.text)
            after = self.state(dest)
            with patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)) as usage:
                denied = api.post(IMPORT, params=params, content=exported.content,
                                  headers=headers | self.auth(reader))
                self.assertEqual(denied.status_code, 403, denied.text)
                replayed = api.post(IMPORT, params=params, headers=headers, content=exported.content)
                self.assertEqual(replayed.status_code, 201, replayed.text)
                self.assertEqual(replayed.json(), completed.json())
                usage.assert_not_called()
            self.assertEqual(self.state(dest), after)

    def test_import_rolls_back_publication_and_reconciles_interruption(self):
        with self.client() as source:
            token = self.token(source)
            repair = self.mutate(source, token, "/repairs", json=DEVICE)
            for blob in (ORIGINAL, DETACHED):
                self.mutate(source, token, f"/repairs/{repair['id']}/attachments",
                            params={"filename": "sample.bin"}, content=blob)
            response = source.get(EXPORT, headers=self.auth(token))
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.content

        dest = self.destination()
        archive = dest.parent / "interruption.zip"
        archive.write_bytes(payload)
        params = {"destination_shop_id": 1, "revision": 1}
        with TestClient(create_app(workspace=dest), base_url=ORIGIN, follow_redirects=False) as api:
            token = self.token(api)
            headers = self.auth(token) | {"Idempotency-Key": "interruption"}
            before = self.state(dest)
            invalid = api.post(IMPORT, params=params, headers=headers, content=b"invalid archive")
            self.assertEqual(invalid.status_code, 422, invalid.text)
            self.assertEqual(invalid.json()["error"]["code"], "invalid_archive")
            publish = StagedBlob.publish
            calls = []

            def fail_second(upload, shop_id, **options):
                if calls:
                    raise OSError("Synthetic second publication failure")
                calls.append(publish(upload, shop_id, **options))

            with patch.object(StagedBlob, "publish", fail_second):
                failed = api.post(IMPORT, params=params, headers=headers, content=payload)
            self.assertEqual(failed.status_code, 503, failed.text)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.state(dest), before)

        script = """
import os
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from repairlib.app import create_app
from repairlib.storage.blobs import StagedBlob

publish = StagedBlob.publish
def interrupt(upload, shop_id, **options):
    publish(upload, shop_id, **options)
    os._exit(76)
StagedBlob.publish = interrupt
with TestClient(create_app(workspace=Path(sys.argv[1])), base_url="http://127.0.0.1") as api:
    api.post("/api/v1/portable/import", params={"destination_shop_id": 1, "revision": 1},
             headers={"Authorization": "Bearer " + sys.stdin.read(),
                      "Idempotency-Key": "interruption"}, content=Path(sys.argv[2]).read_bytes())
"""
        interrupted = subprocess.run([sys.executable, "-B", "-c", script, str(dest), str(archive)],
                                     input=token, text=True, env=python_env(), capture_output=True, timeout=20)
        self.assertEqual(interrupted.returncode, 76, interrupted.stderr)
        durable, leftovers = self.state(dest)
        self.assertEqual(durable, before[0])
        self.assertEqual(sorted(leftovers.values()), sorted([ORIGINAL, DETACHED]))
        reconciled = subprocess.run([*COMMAND, "reconcile-files", "--workspace", str(dest), "--json"],
                                    env=python_env(), text=True, capture_output=True, timeout=20)
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        self.assertEqual(json.loads(reconciled.stdout),
                         {"checked": 0, "removed_staging": 1, "removed_orphans": 1,
                          "missing": [], "mismatched": []})
        with TestClient(create_app(workspace=dest), base_url=ORIGIN, follow_redirects=False) as api:
            completed = api.post(IMPORT, params=params, headers=headers, content=payload)
            self.assertEqual(completed.status_code, 201, completed.text)
            state = self.state(dest)
            self.assertEqual(sorted(state[1].values()), sorted([ORIGINAL, DETACHED]))
            self.assertEqual(api.post(IMPORT, params=params, headers=headers, content=payload).json(),
                             completed.json())
            self.assertEqual(self.state(dest), state)
