"""Attachment preview: sniffed images inline within the limit, validators and 304."""

from contextlib import closing
from hashlib import sha256
import re
import sqlite3
import unittest

from uuid import uuid4

from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 60
PDF = b"%PDF-1.4\n" + b"synthetic\n" * 10
TEXT = b"plain synthetic notes\n"
MIB = 1024 * 1024
REVALIDATE = "private, no-cache, must-revalidate, no-transform"


class AttachmentPreviewTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def upload(self, client, auth, repair_id, content, *, filename):
        return client.post(f"{API}/{repair_id}/attachments", params={"filename": filename},
                           headers={**auth, "Idempotency-Key": uuid4().hex,
                                    "Content-Type": "application/octet-stream"}, content=content)

    def content(self, client, auth, repair, attachment, headers=None, inline=True):
        url = f"{API}/{repair['id']}/attachments/{attachment['id']}/content"
        return client.get(url, params={"inline": "1"} if inline else {}, headers={**auth, **(headers or {})})

    def test_preview_serves_three_sniffed_types_with_validators(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            for blob, media_type, name in ((JPEG, "image/jpeg", "a.jpg"), (PNG, "image/png", "b.png"),
                                           (WEBP, "image/webp", "c.webp")):
                attachment = self.upload(client, auth, repair["id"], blob, filename=name).json()
                tag = f'"{sha256(blob).hexdigest()}"'
                reply = self.content(client, auth, repair, attachment)
                self.assertEqual((reply.status_code, reply.content, reply.headers["content-type"]),
                                 (200, blob, media_type))
                self.assertTrue(reply.headers["content-disposition"].startswith("inline;"))
                self.assertEqual(reply.headers["etag"], tag)
                self.assertEqual(reply.headers["cache-control"], REVALIDATE)
                self.assertEqual(reply.headers["x-content-type-options"], "nosniff")
                match = self.content(client, auth, repair, attachment, {"If-None-Match": tag})
                self.assertEqual((match.status_code, match.content), (304, b""))
                self.assertEqual(match.headers["etag"], tag)
                self.assertEqual(match.headers["cache-control"], REVALIDATE)
                listed = self.content(client, auth, repair, attachment, {"If-None-Match": f'"0", {tag}'})
                self.assertEqual(listed.status_code, 304)
                other = self.content(client, auth, repair, attachment, {"If-None-Match": '"' + "0" * 64 + '"'})
                self.assertEqual((other.status_code, other.content), (200, blob))
                self.assertEqual(self.content(client, auth, repair, attachment, inline=False).status_code, 200)

    def test_preview_size_boundary_and_unsupported_types_stay_downloads(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET inline_preview_mib = 1 WHERE shop_id = 1")
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            at_limit = PNG + b"\x00" * (MIB - len(PNG))
            over = at_limit + b"\x00"
            fits = self.upload(client, auth, repair["id"], at_limit, filename="fits.png").json()
            large = self.upload(client, auth, repair["id"], over, filename="large.png").json()
            self.assertEqual(self.content(client, auth, repair, fits).status_code, 200)
            refused = self.content(client, auth, repair, large)
            self.assertEqual(refused.status_code, 415)
            self.assertNotIn("etag", refused.headers)
            download = self.content(client, auth, repair, large, inline=False)
            self.assertEqual((download.status_code, download.content), (200, over))
            self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))
            for blob, name in ((PDF, "doc.pdf"), (TEXT, "notes.txt")):
                attachment = self.upload(client, auth, repair["id"], blob, filename=name).json()
                self.assertEqual(self.content(client, auth, repair, attachment).status_code, 415)
                self.assertEqual(self.content(client, auth, repair, attachment,
                                              {"If-None-Match": f'"{sha256(blob).hexdigest()}"'}).status_code, 415)
                download = self.content(client, auth, repair, attachment, inline=False)
                self.assertEqual((download.status_code, download.content), (200, blob))
                self.assertEqual(download.headers["content-type"], "application/octet-stream")

    def test_preview_requires_sign_in_and_stays_inside_the_shop(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            image = self.upload(client, auth, repair["id"], PNG, filename="b.png").json()
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
                db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                           "VALUES (50,2,'outsider','admin','unused')")
                db.execute("INSERT INTO attachments(id,shop_id,repair_id,uploader_id,uploader_username,"
                           "filename,media_type,size_bytes,sha256,storage_name) VALUES "
                           "(50,2,50,50,'outsider','hidden.png','image/png',1,?,?)", ("0" * 64, "f" * 32))
            tag = {"If-None-Match": f'"{sha256(PNG).hexdigest()}"'}
            self.assertEqual(self.content(client, {}, repair, image, tag).status_code, 401)
            for headers in ({}, {"If-None-Match": '"' + "0" * 64 + '"'}):
                foreign = client.get(f"{API}/50/attachments/50/content", params={"inline": "1"},
                                     headers={**auth, **headers})
                self.assertEqual(foreign.status_code, 404)
            borrowed = client.get(f"{API}/{repair['id']}/attachments/50/content", params={"inline": "1"},
                                  headers=auth)
            self.assertEqual(borrowed.status_code, 404)
            page = f"/repairs/{repair['id']}/attachments/{image['id']}/content"
            web = client.get(page, params={"inline": "1"})
            self.assertEqual((web.status_code, web.headers["etag"]), (200, f'"{sha256(PNG).hexdigest()}"'))
            self.assertEqual(web.headers["cache-control"], REVALIDATE)
            cached = client.get(page, params={"inline": "1"}, headers=tag)
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers["cache-control"], REVALIDATE)
            html = client.get("/").text
            token = re.search(r'<input [^>]*form="nav-logout" name="csrf" value="([^"]+)"', html)
            self.assertIsNotNone(token)
            self.assertEqual(client.post("/logout", data={"csrf": token[1]}).status_code, 303)
            refused = client.get(page, params={"inline": "1"}, headers=tag)
            self.assertIn(refused.status_code, (303, 401))
            self.assertNotIn("etag", refused.headers)
        with self.client() as anonymous:
            self.assertIn(anonymous.get(page, params={"inline": "1"}, headers=tag).status_code, (303, 401))


if __name__ == "__main__":
    unittest.main()
