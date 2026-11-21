"""Device photo and one picture per work entry, with parity across web, API and CLI."""

from contextlib import closing
import json
import re
import sqlite3
import subprocess
import sys
import unittest
from uuid import uuid4

from test_attachments import form_fields
from test_baseline import python_env
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret
from test_web_repair_workspace import panel

COMMAND = [sys.executable, "-m", "repairlib"]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00synthetic board photo" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00synthetic device photo" * 8
TEXT = b"not an image at all\n" * 4


class RepairPhotoTests(TokenFixture, unittest.TestCase):
    def start(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        auth = {"Authorization": f"Bearer {token}"}
        repair = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE).json()
        return auth, repair["id"]

    def upload(self, client, auth, repair_id, content, filename="photo.png", **params):
        return client.post(f"{API}/{repair_id}/attachments", params={"filename": filename, **params},
                           headers={**auth, "Idempotency-Key": uuid4().hex,
                                    "Content-Type": "application/octet-stream"}, content=content)

    def counts(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT (SELECT COUNT(*) FROM attachments), (SELECT COUNT(*) FROM work_entries)").fetchone()

    def files(self):
        root = self.workspace / "state" / "files"
        return sorted(p for p in root.rglob("*") if p.is_file() and ".staging" not in p.parts)

    def test_device_photo_api_shows_newest_live_photo_and_refuses_non_images(self):
        with self.client() as client:
            auth, repair = self.start(client)
            page = panel(client.get(f"/repairs/{repair}"), "status")
            self.assertIn('id="device-photo"', page)
            self.assertNotIn("<img", page)
            before = (self.counts(), self.files())
            refused = self.upload(client, auth, repair, TEXT, "notes.txt", role="device")
            self.assertEqual(refused.status_code, 422, refused.text)
            self.assertEqual((self.counts(), self.files()), before)
            self.assertEqual(self.upload(client, auth, repair, PNG, role="bogus").status_code, 422)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE shop_settings SET inline_preview_mib = 1")
            oversized = self.upload(client, auth, repair, PNG + b"\x00" * (1 << 20), role="device")
            self.assertEqual(oversized.status_code, 422, oversized.text)
            self.assertEqual((self.counts(), self.files()), before)
            first = self.upload(client, auth, repair, PNG, role="device").json()
            second = self.upload(client, auth, repair, JPEG, "front.jpg", role="device").json()
            self.assertEqual((first["role"], second["role"]), ("device", "device"))
            header = panel(client.get(f"/repairs/{repair}"), "status")
            self.assertIn(f'/attachments/{second["id"]}/content?inline=1', header)
            self.assertNotIn(f'/attachments/{first["id"]}/content', header)
            self.assertIn('href="#device-photo-zoom"', header)
            detached = client.post(f"{API}/{repair}/attachments/{second['id']}/detach",
                                   headers={**auth, "Idempotency-Key": uuid4().hex}, json={"revision": 1, "detached": True})
            self.assertEqual(detached.status_code, 200, detached.text)
            self.assertIn(f'/attachments/{first["id"]}/content?inline=1',
                          panel(client.get(f"/repairs/{repair}"), "status"))

    def test_device_photo_refuses_another_shops_repair(self):
        with self.client() as client:
            auth, _ = self.start(client)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other synthetic shop')")
                foreign = db.execute("INSERT INTO repairs(shop_id, reported_issue) VALUES (2, 'Other') RETURNING id").fetchone()[0]
            before = (self.counts(), self.files())
            self.assertEqual(self.upload(client, auth, foreign, PNG, role="device").status_code, 404)
            self.assertEqual((self.counts(), self.files()), before)

    def test_web_header_upload_sets_the_device_photo(self):
        with self.client() as client:
            _, repair = self.start(client)
            page = client.get(f"/repairs/{repair}")
            action = f"/repairs/{repair}/photo"
            self.assertRegex(page.text, rf'<form[^>]*action="{action}"[^>]*enctype="multipart/form-data"')
            self.assertRegex(page.text, r'<input[^>]*name="file"[^>]*accept="image/\*"')
            fields = form_fields(page, action)
            sent = client.post(action, data=fields, files={"file": ("phone.jpg", JPEG, "image/jpeg")})
            self.assertEqual(sent.status_code, 303, sent.text)
            self.assertIn("/content?inline=1", panel(client.get(f"/repairs/{repair}"), "status"))
            refused = client.post(action, data=form_fields(client.get(f"/repairs/{repair}"), action),
                                  files={"file": ("notes.txt", TEXT, "text/plain")})
            self.assertEqual(refused.status_code, 422)

    def test_composer_adds_one_picture_with_its_entry_atomically(self):
        with self.client() as client:
            _, repair = self.start(client)
            action = f"/repairs/{repair}/entries"
            page = client.get(f"/repairs/{repair}")
            self.assertRegex(page.text, rf'<form[^>]*action="{action}"[^>]*enctype="multipart/form-data"')
            fields = form_fields(page, action)
            before = self.counts()
            bad = client.post(action, data={**fields, "body": "Chip replaced", "kind": "work"},
                              files={"file": ("notes.txt", TEXT, "text/plain")})
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(self.counts(), before)
            data = {**fields, "body": "Replaced U12; photo of the new chip", "kind": "work"}
            sent = client.post(action, data=data, files={"file": ("chip.png", PNG, "image/png")})
            self.assertEqual(sent.status_code, 303, sent.text)
            replay = client.post(action, data=data, files={"file": ("chip.png", PNG, "image/png")})
            self.assertEqual(replay.status_code, 303)
            self.assertEqual(self.counts(), (before[0] + 1, before[1] + 1))
            with closing(sqlite3.connect(self.db)) as db:
                entry, linked = db.execute(
                    "SELECT e.id, l.attachment_id FROM work_entries e JOIN attachment_links l "
                    "ON l.shop_id = e.shop_id AND l.entry_id = e.id WHERE e.body LIKE 'Replaced U12%'").fetchone()
            log = panel(client.get(f"/repairs/{repair}"), "log")
            bubble = log.split(f'id="entry-{entry}"', 1)[1].split("</li>", 1)[0]
            self.assertIn(f'/attachments/{linked}/content?inline=1', bubble)
            self.assertIn(f'href="#entry-picture-{entry}"', bubble)
            plain = client.post(action, data={**form_fields(client.get(f"/repairs/{repair}"), action),
                                              "body": "Text only", "kind": "note"})
            self.assertEqual(plain.status_code, 303)
            self.assertEqual(self.counts(), (before[0] + 1, before[1] + 2))

    def test_cli_sets_device_photo_and_adds_entry_with_picture(self):
        with self.client() as client:
            _, repair = self.start(client)
        photo = self.workspace.parent / "front.jpg"
        photo.write_bytes(JPEG)
        chip = self.workspace.parent / "chip.png"
        chip.write_bytes(PNG)

        def cli(*args):
            result = subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                                    env=python_env(), text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        attached = cli("attach", str(repair), str(photo), "--photo", "--key", "cli-photo")
        self.assertEqual(attached["role"], "device")
        added = cli("add-entry", str(repair), "--body", "Board after cleaning", "--file", str(chip), "--key", "cli-entry")
        self.assertEqual(added["entry_ids"], [added["entry_ids"][0]])
        self.assertEqual(added["media_type"], "image/png")
        refused = subprocess.run([*COMMAND, "repairs", "attach", str(repair), str(self.workspace.parent / "front.jpg"),
                                  "--photo", "--key", "cli-photo-2", "--name", "x", "--workspace", str(self.workspace)],
                                 env=python_env(), text=True, capture_output=True, timeout=30)
        self.assertEqual(refused.returncode, 0, refused.stderr)
        text = self.workspace.parent / "notes.txt"
        text.write_bytes(TEXT)
        before = self.counts()
        bad = subprocess.run([*COMMAND, "repairs", "add-entry", str(repair), "--body", "x", "--file", str(text),
                              "--key", "cli-bad", "--workspace", str(self.workspace)],
                             env=python_env(), text=True, capture_output=True, timeout=30)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.counts(), before)
        with self.client() as client:
            self.sign_in(client)
            header = panel(client.get(f"/repairs/{repair}"), "status")
            self.assertIn("/content?inline=1", header)
            self.assertTrue(re.search(r'id="entry-\d+"', panel(client.get(f"/repairs/{repair}"), "log")))


if __name__ == "__main__":
    unittest.main()
