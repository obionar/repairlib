"""S3 attachment limits as bounded shop settings and admin purge after 30 detached days."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from argon2 import PasswordHasher

from repairlib.storage import blobs, initialization
from test_attachments import ORIGINAL, PNG, form_fields
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import PASSWORD, TokenFixture
from test_users import ACCOUNT, hidden, issued_secret

SETTINGS = "/api/v1/settings"
MIB = 1024 * 1024


class RetentionTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        return client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE).json()

    def upload(self, client, auth, repair_id, content, filename="file.bin"):
        return client.post(f"{API}/{repair_id}/attachments", params={"filename": filename},
                           headers={**auth, "Idempotency-Key": uuid4().hex}, content=content)

    def detach(self, client, auth, repair_id, attachment):
        reply = client.post(f"{API}/{repair_id}/attachments/{attachment['id']}/detach",
                            headers={**auth, "Idempotency-Key": uuid4().hex},
                            json={"revision": attachment["revision"], "detached": True})
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    def age(self, attachment_id, days):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE attachments SET detached_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
                       "WHERE id = ?", (f"-{days} days", attachment_id))

    def stored_files(self):
        root = self.workspace / "state" / "files"
        return sorted(p for p in root.rglob("*") if p.is_file() and ".staging" not in p.parts)

    def test_bounded_upload_and_preview_settings_apply_immediately(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            current = client.get(SETTINGS, headers=auth).json()
            self.assertEqual((current["max_upload_mib"], current["inline_preview_mib"]), (256, 8))
            for bad in ({"max_upload_mib": 0}, {"max_upload_mib": 2049}, {"inline_preview_mib": 65},
                        {"inline_preview_mib": 0}):
                reply = client.patch(SETTINGS, headers={**auth, "Idempotency-Key": uuid4().hex},
                                     json={"revision": current["revision"], **bad})
                self.assertEqual(reply.status_code, 422, bad)
            changed = client.patch(SETTINGS, headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": current["revision"], "max_upload_mib": 1,
                                         "inline_preview_mib": 1})
            self.assertEqual(changed.status_code, 200, changed.text)
            repair = self.repair(client, auth)
            self.assertEqual(self.upload(client, auth, repair["id"], b"x" * (MIB + 1)).status_code, 413)
            exact = self.upload(client, auth, repair["id"], PNG + b"\0" * (MIB - len(PNG)), "large.png")
            self.assertEqual(exact.status_code, 201, exact.text)
            self.assertFalse(self.upload(client, auth, repair["id"], PNG + b"\0" * MIB, "big.png").status_code == 201)
            listed = client.get(f"{API}/{repair['id']}/attachments", headers=auth).json()["items"][0]
            self.assertTrue(listed["previewable"])
            client.patch(SETTINGS, headers={**auth, "Idempotency-Key": uuid4().hex},
                         json={"revision": changed.json()["revision"], "max_upload_mib": 4,
                               "inline_preview_mib": 1})
            bigger = self.upload(client, auth, repair["id"], PNG + b"\0" * (2 * MIB), "bigger.png").json()
            self.assertEqual((bigger["media_type"], bigger["previewable"]), ("image/png", False))
            self.assertEqual(client.get(f"{API}/{repair['id']}/attachments/{bigger['id']}/content",
                                        params={"inline": "1"}, headers=auth).status_code, 415)

            page = client.get("/settings")
            self.assertIn('name="max_upload_mib"', page.text)
            form = {"csrf": hidden(page, "csrf"), "key": hidden(page, "key"), "revision": hidden(page, "revision"),
                    "name": "Synthetic", "timezone": "UTC", "max_upload_mib": "64", "inline_preview_mib": "2"}
            self.assertEqual(client.post("/settings", data=form).status_code, 303)
            self.assertEqual(client.get(SETTINGS, headers=auth).json()["max_upload_mib"], 64)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='settings.update'"),
                         [(3,)])

    def test_admin_purge_after_thirty_detached_days_keeps_metadata_and_history(self):
        with self.client() as owner, self.client() as technician:
            _, auth = self.admin_auth(owner)
            owner.post("/api/v1/users", headers={**auth, "Idempotency-Key": uuid4().hex}, json={
                "username": "technician", "role": "technician", "password": PASSWORD})
            repair = self.repair(owner, auth)
            attachment = self.upload(owner, auth, repair["id"], ORIGINAL).json()
            purge_url = f"{API}/{repair['id']}/attachments/{attachment['id']}/purge"
            self.assertEqual(owner.post(purge_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                                        json={"revision": 1}).status_code, 409)  # still attached
            detached = self.detach(owner, auth, repair["id"], attachment)
            self.age(attachment["id"], 29)
            early = owner.post(purge_url, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"revision": detached["revision"]})
            self.assertEqual(early.status_code, 409)
            self.age(attachment["id"], 31)

            self.sign_in(technician, "technician")
            tech_token = issued_secret(technician.post(ACCOUNT, data={
                "csrf": hidden(technician.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Technician", "scope": "write", "password": PASSWORD}))
            denied = technician.post(purge_url, headers={"Authorization": f"Bearer {tech_token}",
                                                         "Idempotency-Key": uuid4().hex},
                                     json={"revision": detached["revision"]})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(len(self.stored_files()), 1)

            key = uuid4().hex
            purged = owner.post(purge_url, headers={**auth, "Idempotency-Key": key},
                                json={"revision": detached["revision"]})
            self.assertEqual(purged.status_code, 200, purged.text)
            self.assertEqual((purged.json()["purged"], purged.json()["sha256"], purged.json()["filename"]),
                             (True, attachment["sha256"], attachment["filename"]))
            self.assertEqual(owner.post(purge_url, headers={**auth, "Idempotency-Key": key},
                                        json={"revision": detached["revision"]}).json(), purged.json())
            self.assertEqual(self.stored_files(), [])
            content = owner.get(f"{API}/{repair['id']}/attachments/{attachment['id']}/content", headers=auth)
            self.assertEqual(content.status_code, 410)
            restore = owner.post(f"{API}/{repair['id']}/attachments/{attachment['id']}/detach",
                                 headers={**auth, "Idempotency-Key": uuid4().hex},
                                 json={"revision": purged.json()["revision"], "detached": False})
            self.assertEqual(restore.status_code, 409)
            listed = owner.get(f"{API}/{repair['id']}/attachments", headers=auth).json()["items"]
            self.assertEqual([(item["id"], item["purged"]) for item in listed], [(attachment["id"], True)])
        operations = [row[0] for row in self.snapshot(
            "SELECT operation FROM audit_events WHERE entity_type='attachment' ORDER BY id")]
        self.assertEqual(operations, ["attachment.create", "attachment.detach", "attachment.purge"])
        result = subprocess.run([*COMMAND, "reconcile-files", "--workspace", str(self.workspace), "--json"],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["missing"], [])

    def test_crash_after_purge_commit_leaves_file_for_reconciliation(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            attachment = self.upload(client, auth, repair["id"], ORIGINAL).json()
            detached = self.detach(client, auth, repair["id"], attachment)
            self.age(attachment["id"], 40)
            with patch.object(blobs, "remove_blob", side_effect=OSError("Synthetic crash")):
                failure = client.post(f"{API}/{repair['id']}/attachments/{attachment['id']}/purge",
                                      headers={**auth, "Idempotency-Key": uuid4().hex},
                                      json={"revision": detached["revision"]})
                self.assertEqual(failure.status_code, 500)
                self.assertEqual(failure.json()["error"]["code"], "server_error")
        self.assertEqual(self.snapshot("SELECT purged_at IS NOT NULL FROM attachments"), [(1,)])
        self.assertEqual(len(self.stored_files()), 1)
        result = subprocess.run([*COMMAND, "reconcile-files", "--workspace", str(self.workspace), "--json"],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)["removed_orphans"], json.loads(result.stdout)["missing"]),
                         (1, []))
        self.assertEqual(self.stored_files(), [])

    def test_browser_purge_and_real_http_cli_settings_and_purge(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            first = self.upload(client, auth, repair["id"], ORIGINAL).json()
            second = self.upload(client, auth, repair["id"], ORIGINAL, "second.bin").json()
            for item in (first, second):
                detached = self.detach(client, auth, repair["id"], item)
                self.age(item["id"], 31)
            page = client.get(f"/repairs/{repair['id']}")
            action = f"/repairs/{repair['id']}/attachments/{first['id']}/purge"
            self.assertEqual(client.post(action, data=form_fields(page, action)).status_code, 303)
            self.assertIn("Purged", client.get(f"/repairs/{repair['id']}").text)


            def cli(*args):
                result = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace), "--json"],
                                        text=True, capture_output=True, timeout=10, env=python_env())
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            current = cli("settings", "show")
            changed = cli("settings", "edit", "--revision", str(current["revision"]), "--key", uuid4().hex,
                          "--max-upload-mib", "128", "--inline-preview-mib", "4")
            self.assertEqual((changed["max_upload_mib"], changed["inline_preview_mib"]), (128, 4))
            purged = cli("repairs", "purge-attachment", str(repair["id"]), str(second["id"]),
                         "--revision", str(detached["revision"]), "--key", uuid4().hex)
            self.assertTrue(purged["purged"])
        self.assertEqual(self.stored_files(), [])

    def test_populated_schema_twelve_upgrade_and_failed_migration_rollback(self):
        restored = self.workspace.parent / "restore"
        (restored / "state").mkdir(parents=True)
        copy = restored / "state" / self.db.name
        initialization.initialize_database(copy, "Previous shop", "UTC", "owner",
                                           PasswordHasher().hash(PASSWORD), target=12)
        with closing(sqlite3.connect(copy)) as db, db:
            db.execute("INSERT INTO repairs(shop_id,reported_issue) VALUES (1,'Prior issue')")
            db.execute("INSERT INTO attachments(shop_id,repair_id,uploader_id,uploader_username,filename,"
                       "media_type,size_bytes,sha256,storage_name) VALUES "
                       "(1,1,1,'owner','prior.bin','application/octet-stream',1,?,?)", ("0" * 64, "a" * 32))
        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic migration failure")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(copy)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (12,))
        result = subprocess.run([*COMMAND, "migrate", "--workspace", str(restored)],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT max_upload_mib, inline_preview_mib FROM shop_settings").fetchall(),
                             [(256, 8)])
            self.assertEqual(db.execute("SELECT filename, purged_at FROM attachments").fetchall(),
                             [("prior.bin", None)])


if __name__ == "__main__":
    unittest.main()
