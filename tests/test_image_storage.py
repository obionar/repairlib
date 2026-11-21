"""Images live under shop-N/images/<repair id>/ and survive every file walker."""

from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import unittest
from uuid import uuid4

from repairlib.doctor import diagnose_workspace
from repairlib.domain.attachments import reconcile_files
from repairlib.domain.setup import initialize_workspace
from repairlib.storage.blobs import blob_path
from test_baseline import python_env
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, PASSWORD, TokenFixture, issued_secret

COMMAND = [sys.executable, "-m", "repairlib"]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00synthetic image bytes" * 8
ORIGINAL = b"\x00\xffSynthetic non-image bytes\r\n" * 8


class ImageStorageTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}
            self.repairs = [client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE).json()["id"]
                            for _ in range(2)]
            self.image = self.upload(client, auth, self.repairs[1], PNG, "board.png")
            self.other = self.upload(client, auth, self.repairs[1], ORIGINAL, "notes.bin")
            self.auth = auth

    def upload(self, client, auth, repair_id, content, filename):
        created = client.post(f"{API}/{repair_id}/attachments", params={"filename": filename},
                              headers={**auth, "Idempotency-Key": uuid4().hex,
                                       "Content-Type": "application/octet-stream"}, content=content)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def stored(self, workspace, attachment_id):
        with closing(sqlite3.connect(workspace / "state" / "repairlib.sqlite3")) as db:
            return db.execute("SELECT shop_id, repair_id, storage_folder || storage_name FROM attachments WHERE id = ?",
                              (attachment_id,)).fetchone()

    def cli(self, *args):
        return subprocess.run([*COMMAND, *args], env=python_env(), text=True, capture_output=True, timeout=30)

    def test_images_go_under_the_repair_folder_and_other_files_stay_flat(self):
        shop, repair, name = self.stored(self.workspace, self.image["id"])
        self.assertRegex(name, rf"^images/{repair}/[0-9a-f]{{32}}$")
        path = self.workspace / "state" / "files" / f"shop-{shop}" / name
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(blob_path(self.workspace, shop, name), path)
        _, _, flat = self.stored(self.workspace, self.other["id"])
        self.assertRegex(flat, r"^[0-9a-f]{32}$")
        with self.client() as client:
            content = client.get(f"{API}/{repair}/attachments/{self.image['id']}/content", headers=self.auth)
            self.assertEqual(content.content, PNG)

    def test_nested_names_refuse_traversal_and_zero_ids(self):
        good = "0" * 32
        for name in (f"images/0/{good}", f"images/../{good}", f"images/1/../{good}", f"images/1/{good}/x",
                     f"/images/1/{good}", f"images/01/{good}", f"photos/1/{good}", f"images/1//{good}"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                blob_path(self.workspace, 1, name)

    def test_reconcile_and_doctor_walk_image_folders(self):
        shop, repair, name = self.stored(self.workspace, self.image["id"])
        folder = self.workspace / "state" / "files" / f"shop-{shop}" / "images" / str(repair)
        orphan = folder / ("a" * 32)
        orphan.write_bytes(b"never acknowledged")
        orphan.chmod(0o600)
        report = diagnose_workspace(self.workspace)
        self.assertEqual((report.unsafe_storage, report.orphans), (0, 1))
        self.assertEqual(report.attachment_issues, {})
        result = reconcile_files(self.workspace)
        self.assertEqual((result["missing"], result["mismatched"], result["removed_orphans"]), ([], [], 1))
        self.assertFalse(orphan.exists())
        self.assertEqual((folder / name.rsplit("/", 1)[1]).read_bytes(), PNG)
        (folder / name.rsplit("/", 1)[1]).write_bytes(b"tampered")
        self.assertIn(self.image["id"], reconcile_files(self.workspace)["mismatched"])

    def test_backup_restore_keeps_the_image_folder(self):
        archive = self.workspace.parent / "backup.zip"
        created = self.cli("backup", "create", "--output", str(archive), "--workspace", str(self.workspace))
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(self.cli("backup", "verify", str(archive)).returncode, 0)
        destination = self.workspace.parent / "restored"
        restored = self.cli("backup", "restore", str(archive), "--workspace", str(destination))
        self.assertEqual(restored.returncode, 0, restored.stderr)
        shop, repair, name = self.stored(destination, self.image["id"])
        self.assertEqual((destination / "state" / "files" / f"shop-{shop}" / name).read_bytes(), PNG)
        report = diagnose_workspace(destination)
        self.assertEqual((report.unsafe_storage, report.orphans, report.attachment_issues), (0, 0, {}))

    def test_portable_import_files_images_under_the_new_repair_id(self):
        archive = self.workspace.parent / "portable.zip"
        exported = self.cli("portable", "export", str(archive), "--workspace", str(self.workspace), "--json")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        dest = self.workspace.parent / "destination"
        initialize_workspace(dest, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
        with closing(sqlite3.connect(dest / "state" / "repairlib.sqlite3")) as db, db:
            # Occupied shop 1 forces different repair IDs in the explicit destination shop 2.
            db.executemany("INSERT INTO repairs(shop_id, reported_issue) VALUES (1, ?)",
                           [("Outside repair",)] * 5)
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Explicit destination')")
            db.execute("INSERT INTO shop_settings(shop_id, timezone) VALUES (2, 'UTC')")
            db.execute("INSERT INTO users(shop_id, username, password_hash, role) "
                       "SELECT 2, username, password_hash, role FROM users WHERE shop_id=1")
            db.execute("UPDATE local_workspace SET shop_id=2")
        imported = self.cli("portable", "import", str(archive), "--workspace", str(dest), "--json",
                            "--destination-shop-id", "2", "--revision", "1", "--key", "image-import")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(json.loads(imported.stdout)["destination_shop_id"], 2)
        with closing(sqlite3.connect(dest / "state" / "repairlib.sqlite3")) as db:
            rows = db.execute("SELECT shop_id, repair_id, storage_folder || storage_name, media_type FROM attachments").fetchall()
        image, = [row for row in rows if row[3] == "image/png"]
        self.assertEqual(image[0], 2)
        self.assertNotEqual(image[1], self.repairs[1])
        self.assertRegex(image[2], rf"^images/{image[1]}/[0-9a-f]{{32}}$")
        self.assertEqual((dest / "state" / "files" / "shop-2" / image[2]).read_bytes(), PNG)
        report = diagnose_workspace(dest)
        self.assertEqual((report.unsafe_storage, report.orphans, report.attachment_issues), (0, 0, {}))


if __name__ == "__main__":
    unittest.main()
