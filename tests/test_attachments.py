"""S3 repair attachment gates: original bytes, limits, isolation and crash windows."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import re
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from argon2 import PasswordHasher

from repairlib.adapters.api import attachments as attachment_api
from repairlib.domain import attachments as attachment_domain
from repairlib.lifecycle.workspace import maintenance_lock
from repairlib.storage import attachments as attachment_storage, initialization
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_repairs import API, DEVICE
from test_tokens import PASSWORD, TokenFixture
from test_users import ACCOUNT, hidden, issued_secret

ORIGINAL = b"\x00\xffSynthetic original bytes\r\n\x1a" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def form_fields(page, action):
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', page.text, re.S)
    if not match:
        raise AssertionError(f"Missing form for {action}")
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match[1]))


class AttachmentTests(TokenFixture, unittest.TestCase):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def upload(self, client, auth, repair_id, content, *, key=None, filename="report.bin", **params):
        return client.post(f"{API}/{repair_id}/attachments", params={"filename": filename, **params},
                           headers={**auth, "Idempotency-Key": key or uuid4().hex,
                                    "Content-Type": "application/octet-stream"},
                           content=content)

    def stored_files(self):
        root = self.workspace / "state" / "files"
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []

    def test_api_original_bytes_hash_replay_download_preview_and_restart(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            other = self.repair(client, auth)
            entry = client.post(f"{API}/{repair['id']}/entries",
                                headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"kind": "test", "body": "Measured the synthetic rail"}).json()
            foreign = client.post(f"{API}/{other['id']}/entries",
                                  headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"kind": "note", "body": "Other repair"}).json()
            key = uuid4().hex
            created = self.upload(client, auth, repair["id"], ORIGINAL, key=key,
                                  filename="../nested/Μέτρηση report.bin", entry_id=entry["id"])
            self.assertEqual(created.status_code, 201, created.text)
            attachment = created.json()
            self.assertEqual((attachment["filename"], attachment["size_bytes"], attachment["sha256"],
                              attachment["media_type"], attachment["entry_id"], attachment["previewable"]),
                             ("Μέτρηση report.bin", len(ORIGINAL), sha256(ORIGINAL).hexdigest(),
                              "application/octet-stream", entry["id"], False))
            self.assertEqual(attachment["uploader_username"], "owner")
            replay = self.upload(client, auth, repair["id"], ORIGINAL, key=key,
                                 filename="../nested/Μέτρηση report.bin", entry_id=entry["id"])
            self.assertEqual((replay.status_code, replay.json()), (201, attachment))
            mismatch = self.upload(client, auth, repair["id"], ORIGINAL + b"!", key=key,
                                   filename="../nested/Μέτρηση report.bin", entry_id=entry["id"])
            self.assertEqual(mismatch.status_code, 409)
            files = self.stored_files()
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), ORIGINAL)
            self.assertEqual(files[0].parent.name, "shop-1")
            self.assertRegex(files[0].name, r"\A[0-9a-f]{32}\Z")
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            wrong_entry = self.upload(client, auth, repair["id"], ORIGINAL, entry_id=foreign["id"])
            self.assertEqual(wrong_entry.status_code, 422)

            url = f"{API}/{repair['id']}/attachments/{attachment['id']}"
            download = client.get(f"{url}/content", headers=auth)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, ORIGINAL)
            self.assertEqual(download.headers["content-type"], "application/octet-stream")
            self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))
            self.assertIn("filename*=UTF-8''%CE%9C", download.headers["content-disposition"])
            self.assertEqual(download.headers["x-content-type-options"], "nosniff")
            self.assertEqual(client.get(f"{url}/content", params={"inline": "1"}, headers=auth).status_code, 415)

            image = self.upload(client, auth, repair["id"], PNG, filename="board.png").json()
            self.assertEqual((image["media_type"], image["previewable"]), ("image/png", True))
            inline = client.get(f"{API}/{repair['id']}/attachments/{image['id']}/content",
                                params={"inline": "1"}, headers=auth)
            self.assertEqual((inline.status_code, inline.headers["content-type"], inline.content),
                             (200, "image/png", PNG))
            self.assertTrue(inline.headers["content-disposition"].startswith("inline;"))
            disguised = self.upload(client, auth, repair["id"], SVG, filename="vector.png").json()
            self.assertEqual((disguised["media_type"], disguised["previewable"]),
                             ("application/octet-stream", False))
            self.assertEqual(client.get(f"{API}/{repair['id']}/attachments/{disguised['id']}/content",
                                        params={"inline": "1"}, headers=auth).status_code, 415)
            self.assertEqual(client.get(f"{API}/{other['id']}/attachments/{attachment['id']}/content",
                                        headers=auth).status_code, 404)

        with self.client() as restarted:
            _, auth = self.admin_auth(restarted)
            listing = restarted.get(f"{API}/{repair['id']}/attachments", headers=auth)
            self.assertEqual(listing.status_code, 200)
            self.assertEqual([item["id"] for item in listing.json()["items"]],
                             [disguised["id"], image["id"], attachment["id"]])
            self.assertEqual(restarted.get(f"{url}/content", headers=auth).content, ORIGINAL)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='attachment.create'"),
                         [(3,)])
        self.assertNotIn(PASSWORD, json.dumps(self.snapshot("SELECT changes FROM audit_events")))

    def test_download_keeps_the_open_original_and_refuses_linked_storage(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair_id = self.repair(client, auth)["id"]
            result = self.upload(client, auth, repair_id, ORIGINAL)
            self.assertEqual(result.status_code, 201, result.text)
            url = f"{API}/{repair_id}/attachments/{result.json()['id']}/content"
            path, = self.stored_files()
            replacement = b"x" * len(ORIGINAL)
            response = attachment_api.file_response

            def replace_path_after_open(found, source, **options):
                path.rename(path.with_suffix(".saved"))
                path.write_bytes(replacement)
                return response(found, source, **options)

            with patch.object(attachment_api, "file_response", side_effect=replace_path_after_open):
                download = client.get(url, headers=auth)
            self.assertEqual((download.status_code, download.content), (200, ORIGINAL))

            # A private external directory could also contain an unrelated file
            # with the right name and byte length. Never follow the directory.
            shop = path.parent
            outside = self.workspace.parent / "other-files"
            shop.rename(outside)
            shop.symlink_to(outside, target_is_directory=True)
            refused = client.get(url, headers=auth)
            self.assertEqual(refused.status_code, 503, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "file_unavailable")
            self.assertEqual((outside / path.name).read_bytes(), replacement)

    def test_low_disk_refuses_declared_uploads_before_staging_and_cleans_streams(self):
        from types import SimpleNamespace

        reserve = 1 << 30
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair_id = self.repair(client, auth)["id"]
            route = f"/repairs/{repair_id}/attachments"
            form = form_fields(client.get(f"/repairs/{repair_id}"), route)
            before = self.metadata_snapshot()
            with patch("shutil.disk_usage", return_value=SimpleNamespace(free=reserve + 1)):
                refused = self.upload(client, auth, repair_id, b"xx")
                self.assertEqual((refused.status_code, refused.json().get("error", {}).get("code")),
                                 (507, "insufficient_storage"))
                browser = client.post(route, data=form, files={"file": ("a.bin", b"xx")})
                self.assertEqual(browser.status_code, 507)
                self.assertIn("disk space", browser.text)
            self.assertFalse((self.workspace / "state" / "files" / ".staging").exists())

            with patch("shutil.disk_usage", side_effect=[SimpleNamespace(free=reserve + 10),
                                                         SimpleNamespace(free=reserve + 1)]):
                streamed = self.upload(client, auth, repair_id, iter([b"xx"]))
                self.assertEqual(streamed.status_code, 507)
            self.assertEqual(self.stored_files(), [])
            self.assertEqual(self.metadata_snapshot(), before)

    def test_limits_permissions_shop_and_archive_refusals_leave_no_files(self):
        with self.client() as owner, self.client() as viewer:
            _, auth = self.admin_auth(owner)
            repair = self.repair(owner, auth)
            with patch.object(attachment_domain, "MAX_UPLOAD_BYTES", 32):
                declared = self.upload(owner, auth, repair["id"], b"x" * 33)
                self.assertEqual(declared.status_code, 413)

                def chunks():
                    yield b"x" * 20
                    yield b"x" * 20

                streamed = owner.post(f"{API}/{repair['id']}/attachments", params={"filename": "a.bin"},
                                      headers={**auth, "Idempotency-Key": uuid4().hex}, content=chunks())
                self.assertEqual(streamed.status_code, 413)
            self.assertEqual(self.upload(owner, auth, repair["id"], b"").status_code, 422)
            self.assertEqual(self.upload(owner, auth, repair["id"], b"x", filename="").status_code, 422)
            self.assertEqual(self.upload(owner, auth, repair["id"], b"x", filename="bad\x00.bin").status_code, 422)
            self.assertEqual(owner.post(f"{API}/{repair['id']}/attachments", params={"filename": "a.bin"},
                                        headers=auth, content=b"x").status_code, 422)
            self.assertEqual(self.upload(owner, {}, repair["id"], b"x").status_code, 401)

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
                db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                           "VALUES (50,2,'outsider','admin','unused')")
                db.execute("INSERT INTO attachments(id,shop_id,repair_id,uploader_id,uploader_username,"
                           "filename,media_type,size_bytes,sha256,storage_name) VALUES "
                           "(50,2,50,50,'outsider','hidden.bin','application/octet-stream',1,?,?)",
                           ("0" * 64, "f" * 32))
            self.assertEqual(self.upload(owner, auth, 50, b"x").status_code, 404)
            self.assertEqual(owner.get(f"{API}/50/attachments", headers=auth).status_code, 404)
            self.assertEqual(owner.get(f"{API}/50/attachments/50/content", headers=auth).status_code, 404)
            self.assertEqual(owner.get(f"{API}/{repair['id']}/attachments/50", headers=auth).status_code, 404)

            self.sign_in(viewer, "viewer")
            viewer_token = issued_secret(viewer.post(ACCOUNT, data={
                "csrf": hidden(viewer.get(ACCOUNT), "csrf"), "key": uuid4().hex,
                "name": "Viewer read", "scope": "read", "password": PASSWORD,
            }))
            viewer_auth = {"Authorization": f"Bearer {viewer_token}"}
            self.assertEqual(self.upload(viewer, viewer_auth, repair["id"], b"x").status_code, 403)
            self.assertEqual(viewer.get(f"{API}/{repair['id']}/attachments", headers=viewer_auth).status_code, 200)

            archived = owner.post(f"{API}/{repair['id']}/archive",
                                  headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 1, "enabled": True})
            self.assertEqual(archived.status_code, 200)
            self.assertEqual(self.upload(owner, auth, repair["id"], b"x").status_code, 409)
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM attachments WHERE shop_id=1"), [(0,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE shop_id=2"), [(0,)])

    def test_detach_and_restore_keep_bytes_with_revisions_and_replay(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            attachment = self.upload(client, auth, repair["id"], ORIGINAL).json()
            url = f"{API}/{repair['id']}/attachments/{attachment['id']}"
            key = uuid4().hex
            detached = client.post(f"{url}/detach", headers={**auth, "Idempotency-Key": key},
                                   json={"revision": 1, "detached": True})
            self.assertEqual(detached.status_code, 200, detached.text)
            self.assertEqual((detached.json()["revision"], detached.json()["detached"]), (2, True))
            self.assertEqual(client.post(f"{url}/detach", headers={**auth, "Idempotency-Key": key},
                                         json={"revision": 1, "detached": True}).json(), detached.json())
            stale = client.post(f"{url}/detach", headers={**auth, "Idempotency-Key": uuid4().hex},
                                json={"revision": 1, "detached": False})
            self.assertEqual(stale.status_code, 409)
            same = client.post(f"{url}/detach", headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"revision": 2, "detached": True})
            self.assertEqual(same.status_code, 409)
            self.assertEqual(client.get(f"{url}/content", headers=auth).content, ORIGINAL)
            self.assertTrue(client.get(f"{API}/{repair['id']}/attachments", headers=auth)
                            .json()["items"][0]["detached"])
            restored = client.post(f"{url}/detach", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 2, "detached": False})
            self.assertEqual((restored.json()["revision"], restored.json()["detached"]), (3, False))
        self.assertEqual([path.read_bytes() for path in self.stored_files()], [ORIGINAL])
        self.assertEqual(self.snapshot("SELECT operation FROM audit_events WHERE entity_type='attachment' "
                                       "ORDER BY id"),
                         [("attachment.create",), ("attachment.detach",), ("attachment.restore",)])

    def test_browser_multipart_upload_preview_detach_without_javascript(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            page = client.get(f"/repairs/{repair['id']}")
            self.assertEqual(page.status_code, 200)
            self.assertIn('enctype="multipart/form-data"', page.text)
            form = form_fields(page, f"/repairs/{repair['id']}/attachments")
            denied = client.post(f"/repairs/{repair['id']}/attachments",
                                 data={**form, "csrf": "invalid"},
                                 files={"file": ("board.png", PNG, "image/png")})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.stored_files(), [])
            for _ in range(2):
                sent = client.post(f"/repairs/{repair['id']}/attachments", data=form,
                                   files={"file": ("board.png", PNG, "image/png")})
                self.assertEqual(sent.status_code, 303, sent.text)
            self.assertEqual(len(self.stored_files()), 1)
            detail = client.get(f"/repairs/{repair['id']}")
            self.assertIn("board.png", detail.text)
            preview = re.search(r'<img src="([^"]+)"', detail.text)
            self.assertIsNotNone(preview)
            image = client.get(preview[1])
            self.assertEqual((image.status_code, image.headers["content-type"], image.content),
                             (200, "image/png", PNG))
            self.assertIn("img-src 'self'", detail.headers["content-security-policy"])
            attachment = client.get(f"{API}/{repair['id']}/attachments", headers=auth).json()["items"][0]
            action = f"/repairs/{repair['id']}/attachments/{attachment['id']}/detach"
            detach = form_fields(detail, action)
            self.assertEqual(detach["revision"], str(attachment["revision"]))
            self.assertEqual(client.post(action, data=detach).status_code, 303)
            after = client.get(f"/repairs/{repair['id']}")
            self.assertIn("Detached files", after.text)
            self.assertIsNone(re.search(r'<img src=', after.text))

    def test_real_http_cli_attach_list_download_and_detach(self):
        source = self.workspace.parent / "synthetic upload.bin"
        source.write_bytes(ORIGINAL)
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            repair = self.repair(client, auth)

            def cli(*args, expect=0):
                result = subprocess.run(
                    [*COMMAND, "repairs", *args, "--workspace", str(self.workspace), "--json"],
                    text=True, capture_output=True, timeout=20, env=python_env(),
                )
                self.assertEqual(result.returncode, expect, result.stderr)
                self.assertNotIn(token, result.stdout + result.stderr)
                return json.loads(result.stdout) if expect == 0 else result

            key = uuid4().hex
            attached = cli("attach", str(repair["id"]), str(source), "--key", key)
            self.assertEqual((attached["filename"], attached["sha256"]),
                             ("synthetic upload.bin", sha256(ORIGINAL).hexdigest()))
            self.assertEqual(cli("attach", str(repair["id"]), str(source), "--key", key), attached)
            listing = cli("attachments", str(repair["id"]))
            self.assertEqual([item["id"] for item in listing["items"]], [attached["id"]])
            output = self.workspace.parent / "downloaded.bin"
            downloaded = cli("download", str(repair["id"]), str(attached["id"]), "--output", str(output))
            self.assertEqual(downloaded["sha256"], attached["sha256"])
            self.assertEqual(output.read_bytes(), ORIGINAL)
            refused = cli("download", str(repair["id"]), str(attached["id"]), "--output", str(output),
                          expect=7)
            # With --json the refusal is one object on stdout, so the reason is asserted there and
            # stderr must stay empty: two surfaces for one failure is what this slice removed.
            self.assertEqual(refused.stderr, "")
            self.assertIn("exists", json.loads(refused.stdout)["error"]["message"])
            detached = cli("detach", str(repair["id"]), str(attached["id"]), "--revision", "1",
                           "--key", uuid4().hex)
            self.assertTrue(detached["detached"])
            restored = cli("detach", str(repair["id"]), str(attached["id"]), "--revision", "2",
                           "--undo", "--key", uuid4().hex)
            self.assertFalse(restored["detached"])

    def attachment_cli(self, client, token):

        def cli(*args):
            result = subprocess.run(
                [*COMMAND, "repairs", *map(str, args), "--workspace", str(self.workspace), "--json"],
                text=True, capture_output=True, timeout=20, env=python_env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        return cli

    def test_caption_web_api_and_http_cli_share_the_reviewed_record(self):
        digest = sha256(ORIGINAL).hexdigest()
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            rid = self.repair(client, auth)["id"]
            aid = self.upload(client, auth, rid, ORIGINAL).json()["id"]
            path, = self.stored_files()
            original_stat = path.stat()
            web = f"/repairs/{rid}/attachments/{aid}/caption"
            api = f"{API}/{rid}/attachments/{aid}"
            page = client.get(f"/repairs/{rid}")
            submitted = {**form_fields(page, web), "caption": "  Board <A>  "}
            self.assertEqual(client.post(web, data=submitted).status_code, 303)
            self.assertEqual(client.post(web, data=submitted).status_code, 303)
            detail = client.get(f"/repairs/{rid}").text
            self.assertIn("Board &lt;A&gt;", detail)
            self.assertNotIn("Board <A>", detail)
            current = client.get(api, headers=auth).json()
            self.assertEqual((current["caption"], current["revision"], current["sha256"]),
                             ("Board <A>", 2, digest))

            stale_form = {**submitted, "key": uuid4().hex, "caption": "Stale"}
            stale = client.post(web, data=stale_form)
            self.assertEqual(stale.status_code, 409)
            rendered = form_fields(stale, web)
            self.assertEqual((rendered["revision"], rendered["key"]),
                             (stale_form["revision"], stale_form["key"]))
            self.assertIn("Stale", stale.text)
            invalid = client.patch(api, headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 2, "caption": "x" * 1001})
            self.assertEqual(invalid.status_code, 422)

            cli = self.attachment_cli(client, token)
            changed = cli("edit-attachment", rid, aid, "--revision", 2,
                          "--caption", "Final caption", "--key", "cli-caption")
            self.assertEqual((changed["caption"], changed["revision"]), ("Final caption", 3))
            clear = {"revision": 3, "caption": ""}
            request_headers = {**auth, "Idempotency-Key": "clear-caption"}
            cleared = client.patch(api, headers=request_headers, json=clear)
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertEqual(cleared.json()["caption"], "")
            self.assertEqual(client.patch(api, headers=request_headers, json=clear).json(), cleared.json())
            self.assertEqual(client.patch(api, headers=request_headers,
                                          json={**clear, "caption": "Different"}).status_code, 409)

        with workspace_client(self.workspace) as (client, _):
            self.assertEqual(client.get(api, headers=auth).json(), cleared.json())
            self.assertEqual(client.get(api + "/content", headers=auth).content, ORIGINAL)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns),
                         (original_stat.st_ino, original_stat.st_mtime_ns))
        events = self.snapshot("SELECT actor_username, source, token_label, changes FROM audit_events "
                               "WHERE operation='attachment.caption' ORDER BY id")
        self.assertEqual([(actor, source, label is not None) for actor, source, label, _ in events],
                         [("owner", "web", False), ("cli", "cli", False), ("owner", "api", True)])
        self.assertEqual([json.loads(changes)["caption"] for _, _, _, changes in events],
                         [{"before": "", "after": "Board <A>"},
                          {"before": "Board <A>", "after": "Final caption"},
                          {"before": "Final caption", "after": ""}])

    def entry(self, client, auth, repair_id, body):
        response = client.post(f"{API}/{repair_id}/entries",
                               headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"kind": "note", "body": body})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_links_web_api_and_http_cli_preserve_independent_associations(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.admin_auth(client)
            rid = self.repair(client, auth)["id"]
            first = self.entry(client, auth, rid, "Initial work")
            second = self.entry(client, auth, rid, "Follow-up work")
            created = self.upload(client, auth, rid, ORIGINAL, entry_id=first).json()
            aid = created["id"]
            path, = self.stored_files()
            stat = path.stat()
            api = f"{API}/{rid}/attachments/{aid}"
            web = f"/repairs/{rid}/attachments/{aid}/links"
            page = client.get(f"/repairs/{rid}")
            link = {**form_fields(page, web), "entry_id": str(second)}
            self.assertEqual(client.post(web, data=link).status_code, 303)
            self.assertEqual(client.post(web, data=link).status_code, 303)
            updated = client.get(api, headers=auth).json()
            self.assertEqual((updated["revision"], updated["entry_id"], updated["entry_ids"]),
                             (2, first, [first, second]))
            self.assertIn(f"Entry {second}", client.get(f"/repairs/{rid}").text)

            request_headers = {**auth, "Idempotency-Key": "remove-initial"}
            unlink = {"revision": 2, "entry_id": first, "linked": False}
            removed = client.post(api + "/links", headers=request_headers, json=unlink)
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual((removed.json()["revision"], removed.json()["entry_ids"]), (3, [second]))
            self.assertEqual(client.post(api + "/links", headers=request_headers, json=unlink).json(),
                             removed.json())
            self.assertEqual(client.post(api + "/links", headers=request_headers,
                                         json={**unlink, "entry_id": second}).status_code, 409)
            fresh = {**auth, "Idempotency-Key": "another-operation"}
            self.assertEqual(client.post(api + "/links", headers=fresh, json=unlink).status_code, 409)
            self.assertEqual(client.post(api + "/links", headers=fresh,
                                         json={**unlink, "revision": 3}).status_code, 409)
            self.assertEqual(client.post(api + "/links", headers=fresh,
                                         json={"revision": 3, "entry_id": second, "linked": True})
                             .status_code, 409)
            cli = self.attachment_cli(client, token)
            self.assertEqual(cli("link-attachment", rid, aid, first, "--revision", 3,
                                 "--key", "cli-link")["entry_ids"], [first, second])
            final = cli("unlink-attachment", rid, aid, second, "--revision", 4, "--key", "cli-unlink")
            self.assertEqual((final["revision"], final["entry_id"], final["entry_ids"]), (5, first, [first]))

        with workspace_client(self.workspace) as (client, _):
            self.assertEqual(client.get(api, headers=auth).json(), final)
            self.assertEqual(client.get(f"{API}/{rid}/attachments", headers=auth).json()["items"], [final])
            self.assertEqual(client.get(api + "/content", headers=auth).content, ORIGINAL)
        self.assertEqual(final["sha256"], sha256(ORIGINAL).hexdigest())
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (stat.st_ino, stat.st_mtime_ns))
        self.assertEqual(self.snapshot("SELECT operation, actor_username, source, token_label FROM audit_events "
                                       "WHERE operation IN ('attachment.link', 'attachment.unlink') ORDER BY id"),
                         [("attachment.link", "owner", "web", None),
                          ("attachment.unlink", "owner", "api", "Workshop CLI"),
                          ("attachment.link", "cli", "cli", None),
                          ("attachment.unlink", "cli", "cli", None)])

    def test_schema_fourteen_initial_link_and_saved_retry_survive_upgrade(self):
        previous = self.workspace.parent / "previous"
        (previous / "state").mkdir(parents=True)
        path = previous / "state" / self.db.name
        initialization.initialize_database(path, "Previous shop", "UTC", "owner",
                                           PasswordHasher().hash(PASSWORD), target=14)
        detached_at = "2026-01-01T00:00:00.000Z"
        operations = ("attachment.create", "attachment.detach")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO repairs(shop_id,reported_issue) VALUES (1,'Prior repair')")
            db.execute("INSERT INTO work_entries(shop_id,repair_id,author_id,author_username,kind,body) "
                       "VALUES (1,1,1,'owner','note','Prior entry')")
            db.execute("INSERT INTO attachments(shop_id,repair_id,entry_id,uploader_id,uploader_username,"
                       "filename,media_type,size_bytes,sha256,storage_name) VALUES "
                       "(1,1,1,1,'owner','report.bin','application/octet-stream',?,?,?)",
                       (len(ORIGINAL), sha256(ORIGINAL).hexdigest(), "a" * 32))
            db.row_factory = sqlite3.Row
            previous_row = db.execute("SELECT * FROM attachments").fetchone()
            legacy = {name: previous_row[name] for name in (
                "id", "repair_id", "entry_id", "filename", "media_type", "size_bytes", "sha256",
                "uploader_id", "uploader_username", "created_at", "revision")}
            legacy.update(detached=False, previewable=False)
            payloads = (
                {"repair_id": 1, "entry_id": 1, "filename": "report.bin",
                 "size_bytes": len(ORIGINAL), "sha256": legacy["sha256"]},
                {"attachment_id": 1, "revision": 1, "detached": True},
            )
            for operation, payload in zip(operations, payloads, strict=True):
                result = legacy if operation == "attachment.create" else {
                    **legacy, "detached": True, "revision": 2, "detached_at": detached_at,
                }
                fingerprint = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                db.execute("INSERT INTO attachment_requests(shop_id,actor_id,operation,request_key,"
                           "fingerprint,attachment_id,result) VALUES(1,1,?,'legacy',?,1,?)",
                           (operation, fingerprint, json.dumps(result)))
            db.execute("UPDATE attachments SET detached_at = ?, revision = 2", (detached_at,))
        files = previous / "state" / "files" / "shop-1"
        files.mkdir(parents=True)
        original = files / ("a" * 32)
        original.write_bytes(ORIGINAL)

        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic migration failure after link backfill")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (14,))
            self.assertEqual(db.execute("SELECT entry_id FROM attachments").fetchone(), (1,))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attachment_links'")
                              .fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachment_requests").fetchone(), (2,))

        for _ in range(2):
            migrated = subprocess.run([*COMMAND, "migrate", "--workspace", str(previous)],
                                      env=python_env(), capture_output=True, text=True, timeout=10)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with workspace_client(previous) as (client, _):
            _, auth = self.admin_auth(client)
            url = f"{API}/1/attachments/1"
            current = client.get(url, headers=auth).json()
            self.assertEqual((current["caption"], current["entry_id"], current["entry_ids"]), ("", 1, [1]))
            upload_replay = self.upload(client, auth, 1, ORIGINAL, key="legacy", entry_id=1)
            self.assertEqual((upload_replay.status_code, upload_replay.json()["entry_ids"]), (201, [1]))
            detach_replay = client.post(url + "/detach", headers={**auth, "Idempotency-Key": "legacy"},
                                        json={"revision": 1, "detached": True})
            self.assertEqual((detach_replay.status_code, detach_replay.json()["entry_ids"],
                              detach_replay.json()["revision"]), (200, [1], 2))
            self.assertEqual(client.get(url, headers=auth).json(), current)
            self.assertEqual(detach_replay.json(), current)
            self.assertEqual(client.get(url + "/content", headers=auth).content, ORIGINAL)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachment_requests").fetchone(), (2,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments").fetchone(), (1,))

    def metadata_snapshot(self):
        return [self.snapshot(f"SELECT * FROM {table} ORDER BY {order}") for table, order in (
            ("attachments", "id"), ("attachment_links", "attachment_id, entry_id"),
            ("attachment_requests", "shop_id, actor_id, operation, request_key"),
            ("audit_events", "id"),
        )]

    def test_metadata_refusals_keep_revisions_links_retries_and_audit_intact(self):
        with self.client() as owner, self.client() as viewer:
            _, auth = self.admin_auth(owner)
            rid = self.repair(owner, auth)["id"]
            other = self.repair(owner, auth)["id"]
            entry = self.entry(owner, auth, rid, "Linked entry")
            wrong_entry = self.entry(owner, auth, other, "Different repair")
            aid = self.upload(owner, auth, rid, ORIGINAL, entry_id=entry).json()["id"]
            url = f"{API}/{rid}/attachments/{aid}"
            web = f"/repairs/{rid}/attachments/{aid}/caption"
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
                db.execute("INSERT INTO shops(id,name) VALUES (2,'Outside')")
                db.execute("INSERT INTO repairs(id,shop_id,reported_issue) VALUES (50,2,'Hidden issue')")
                db.execute("INSERT INTO users(id,shop_id,username,role,password_hash) "
                           "VALUES (50,2,'outsider','admin','unused')")
                db.execute("INSERT INTO work_entries(id,shop_id,repair_id,author_id,author_username,kind,body) "
                           "VALUES (50,2,50,50,'outsider','note','Hidden entry')")
                db.execute("INSERT INTO attachments(id,shop_id,repair_id,uploader_id,uploader_username,"
                           "filename,media_type,size_bytes,sha256,storage_name) VALUES "
                           "(50,2,50,50,'outsider','hidden.bin','application/octet-stream',1,?,?)",
                           ("0" * 64, "f" * 32))
            self.sign_in(viewer, "viewer")
            viewer_token = issued_secret(viewer.post(ACCOUNT, data=self.issue_form(viewer, scope="read")))
            viewer_headers = {"Authorization": f"Bearer {viewer_token}", "Idempotency-Key": "refusal"}
            headers = {**auth, "Idempotency-Key": "refusal"}
            read_only = viewer.get(f"/repairs/{rid}")
            self.assertNotIn("Save caption", read_only.text)
            self.assertNotIn("Unlink work entry", read_only.text)
            self.assertIn("Linked work entries", read_only.text)
            before = self.metadata_snapshot()
            caption = {"revision": 1, "caption": "Forbidden"}
            link = {"revision": 1, "entry_id": entry, "linked": False}
            self.assertEqual(viewer.patch(url, headers=viewer_headers, json=caption).status_code, 403)
            self.assertEqual(viewer.post(url + "/links", headers=viewer_headers, json=link).status_code, 403)
            self.assertEqual(owner.post(web, data={"revision": 1, "caption": "No CSRF"}).status_code, 403)
            for missing in (f"{API}/50/attachments/50", f"{API}/{rid}/attachments/50",
                            f"{API}/{other}/attachments/{aid}"):
                self.assertEqual(owner.patch(missing, headers=headers, json=caption).status_code, 404)
                self.assertEqual(owner.post(missing + "/links", headers=headers, json=link).status_code, 404)
            for invalid in (wrong_entry, 50):
                for linked in (False, True):
                    self.assertEqual(owner.post(url + "/links", headers=headers,
                                               json={**link, "entry_id": invalid, "linked": linked})
                                     .status_code, 422)
            self.assertEqual(self.metadata_snapshot(), before)

            hidden_entry = self.entry(owner, auth, rid, "Soft-deleted entry")
            for target in (entry, hidden_entry):
                removed = owner.post(f"{API}/{rid}/entries/{target}/visibility",
                                     headers={**auth, "Idempotency-Key": uuid4().hex},
                                     json={"revision": 1, "deleted": True, "reason": "Synthetic correction"})
                self.assertEqual(removed.status_code, 200, removed.text)
            before = self.metadata_snapshot()
            self.assertEqual(owner.post(url + "/links", headers=headers,
                                       json={**link, "entry_id": hidden_entry, "linked": True})
                             .status_code, 422)
            self.assertEqual(self.metadata_snapshot(), before)

            unlinked = owner.post(url + "/links", headers=headers, json=link)
            self.assertEqual(unlinked.status_code, 200, unlinked.text)
            self.assertEqual((unlinked.json()["entry_id"], unlinked.json()["entry_ids"]), (None, []))
            self.assertEqual(owner.post(url + "/detach", headers=headers,
                                       json={"revision": 2, "detached": True}).status_code, 200)
            before = self.metadata_snapshot()
            self.assertEqual(owner.patch(url, headers=headers, json={**caption, "revision": 3}).status_code, 409)
            fresh = {**auth, "Idempotency-Key": "new-link"}
            self.assertEqual(owner.post(url + "/links", headers=fresh, json={**link, "revision": 3})
                             .status_code, 409)
            self.assertEqual(owner.post(url + "/links", headers=headers, json=link).json(), unlinked.json())
            self.assertEqual(self.metadata_snapshot(), before)

            self.assertEqual(owner.post(url + "/detach", headers=fresh,
                                       json={"revision": 3, "detached": False}).status_code, 200)
            self.assertEqual(owner.post(f"{API}/{rid}/archive", headers=headers,
                                       json={"revision": 1, "enabled": True}).status_code, 200)
            before = self.metadata_snapshot()
            self.assertEqual(owner.patch(url, headers=headers, json={**caption, "revision": 4}).status_code, 409)
            self.assertEqual(owner.post(url + "/links", headers=fresh, json={**link, "revision": 4})
                             .status_code, 409)
            self.assertEqual(self.metadata_snapshot(), before)
            self.assertEqual(owner.get(url + "/content", headers=auth).content, ORIGINAL)

    def test_link_audit_failure_rolls_back_and_lost_web_redirect_can_replay(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            rid = self.repair(client, auth)["id"]
            entry = self.entry(client, auth, rid, "Link during retry")
            aid = self.upload(client, auth, rid, ORIGINAL).json()["id"]
            api = f"{API}/{rid}/attachments/{aid}"
            web = f"/repairs/{rid}/attachments/{aid}/links"
            form = {**form_fields(client.get(f"/repairs/{rid}"), web), "entry_id": str(entry)}
            before = self.metadata_snapshot()
            with patch.object(attachment_domain, "audit", side_effect=sqlite3.OperationalError):
                self.assertEqual(client.post(web, data=form).status_code, 500)
            self.assertEqual(self.metadata_snapshot(), before)

            with patch("repairlib.adapters.web.repairs.RedirectResponse",
                       side_effect=RuntimeError("Synthetic lost redirect")):
                self.assertEqual(client.post(web, data=form).status_code, 500)
            current = client.get(api, headers=auth).json()
            self.assertEqual((current["entry_ids"], current["revision"]), ([entry], 2))
            committed = self.metadata_snapshot()
            self.assertEqual(client.post(web, data=form).status_code, 303)
            self.assertEqual(self.metadata_snapshot(), committed)

    def test_detached_and_purged_attachment_metadata_remains_readable(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            rid = self.repair(client, auth)["id"]
            entry = self.entry(client, auth, rid, "Retained entry")
            aid = self.upload(client, auth, rid, ORIGINAL, entry_id=entry).json()["id"]
            api = f"{API}/{rid}/attachments/{aid}"
            headers = {**auth, "Idempotency-Key": "retention-metadata"}
            self.assertEqual(client.patch(api, headers=headers,
                                          json={"revision": 1, "caption": "Retained caption"}).status_code, 200)
            self.assertEqual(client.post(api + "/detach", headers=headers,
                                         json={"revision": 2, "detached": True}).status_code, 200)
            detached = client.get(f"/repairs/{rid}").text
            self.assertIn("Retained caption", detached)
            self.assertIn("Linked work entries", detached)
            self.assertNotIn("Save caption", detached)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE attachments SET detached_at='2025-01-01T00:00:00.000Z' WHERE id=?",
                           (aid,))
            purged = client.post(api + "/purge", headers={**auth, "Idempotency-Key": "retention-purge"},
                                 json={"revision": 3})
            self.assertEqual(purged.status_code, 200, purged.text)
            self.assertEqual((purged.json()["caption"], purged.json()["entry_ids"],
                              purged.json()["sha256"]), ("Retained caption", [entry], sha256(ORIGINAL).hexdigest()))
            self.assertEqual(client.get(api + "/content", headers=auth).status_code, 410)
            before = self.metadata_snapshot()
            fresh = {**auth, "Idempotency-Key": "post-purge"}
            self.assertEqual(client.patch(api, headers=fresh,
                                          json={"revision": 4, "caption": "Refused"}).status_code, 409)
            self.assertEqual(client.post(api + "/links", headers=fresh,
                                         json={"revision": 4, "entry_id": entry, "linked": False})
                             .status_code, 409)
            self.assertEqual(self.metadata_snapshot(), before)

    def test_crash_windows_commit_failure_and_stopped_app_reconciliation(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            kept = self.upload(client, auth, repair["id"], ORIGINAL).json()
            lost = self.upload(client, auth, repair["id"], PNG, filename="lost.png").json()

            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("Synthetic failure after file publication")

            with patch.object(attachment_storage, "insert", fail):
                failed = self.upload(client, auth, repair["id"], b"never committed")
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(failed.json()["error"]["code"], "server_error")
        self.assertEqual(len(self.stored_files()), 2)
        files = self.workspace / "state" / "files"
        staging = files / ".staging" / ("b" * 32 + ".part")
        staging.write_bytes(b"partial upload")
        orphan = files / "shop-1" / ("a" * 32)
        orphan.write_bytes(b"published but never committed")
        missing = files / "shop-1" / self.snapshot("SELECT storage_folder || storage_name FROM attachments WHERE id=?",
                                                   (lost["id"],))[0][0]
        missing.unlink()

        with maintenance_lock(self.workspace, exclusive=False):
            busy = subprocess.run([*COMMAND, "reconcile-files", "--workspace", str(self.workspace)],
                                  env=python_env(), capture_output=True, text=True, timeout=10)
            self.assertNotEqual(busy.returncode, 0)
            self.assertTrue(staging.exists())
        result = subprocess.run([*COMMAND, "reconcile-files", "--workspace", str(self.workspace), "--json"],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["removed_staging"], report["removed_orphans"], report["missing"],
                          report["mismatched"]), (1, 1, [lost["id"]], []))
        self.assertFalse(staging.exists() or orphan.exists())
        self.assertEqual([path.read_bytes() for path in self.stored_files()], [ORIGINAL])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM attachments"), [(2,)])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation='files.reconcile'"),
                         [(1,)])
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.assertEqual(client.get(f"{API}/{repair['id']}/attachments/{kept['id']}/content",
                                        headers=auth).content, ORIGINAL)
            self.assertEqual(client.get(f"{API}/{repair['id']}/attachments/{lost['id']}/content",
                                         headers=auth).status_code, 503)

    def test_post_commit_error_keeps_original_for_idempotent_retry(self):
        authorized = attachment_domain.authorized

        @contextmanager
        def fail_after_commit(*args, **kwargs):
            with authorized(*args, **kwargs) as scope:
                yield scope
            if kwargs.get("write"):
                raise sqlite3.OperationalError("Synthetic failure after commit")

        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair_id = self.repair(client, auth)["id"]
            key = "retry-after-commit"
            with patch.object(attachment_domain, "authorized", fail_after_commit):
                self.assertEqual(self.upload(client, auth, repair_id, ORIGINAL, key=key).status_code, 500)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM attachments"), [(1,)])
            self.assertEqual([path.read_bytes() for path in self.stored_files()], [ORIGINAL])
            retry = self.upload(client, auth, repair_id, ORIGINAL, key=key)
            self.assertEqual(retry.status_code, 201, retry.text)
            attachment_id = retry.json()["id"]
            content = client.get(f"{API}/{repair_id}/attachments/{attachment_id}/content", headers=auth)
            self.assertEqual((content.status_code, content.content), (200, ORIGINAL))
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM attachments"), [(1,)])

    def test_reconciliation_refuses_linked_storage_and_preserves_unmanaged_files(self):
        files = self.db.parent / "files"
        files.mkdir()
        stages = files / ".staging"
        outside = self.workspace.parent / "outside"
        outside.mkdir()
        original = outside / ("a" * 32 + ".part")
        original.write_bytes(b"must stay")
        stages.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            attachment_domain.reconcile_files(self.workspace)
        self.assertEqual(original.read_bytes(), b"must stay")

        stages.unlink()
        stages.mkdir()
        unrelated = stages / "notes.txt"
        unrelated.write_bytes(b"keep staging notes")
        abandoned = stages / ("b" * 32 + ".part")
        abandoned.write_bytes(b"uncommitted upload")
        shop = files / "shop-1"
        shop.mkdir()
        also_unrelated = shop / "notes.txt"
        also_unrelated.write_bytes(b"keep shop notes")
        linked = shop / ("c" * 32)
        linked.symlink_to(original)
        orphan = shop / ("d" * 32)
        orphan.write_bytes(b"unreferenced original")
        report = attachment_domain.reconcile_files(self.workspace)
        self.assertEqual((report["removed_staging"], report["removed_orphans"]), (1, 1))
        self.assertFalse(abandoned.exists() or orphan.exists())
        self.assertTrue(linked.is_symlink())
        self.assertEqual(original.read_bytes(), b"must stay")
        self.assertEqual(unrelated.read_bytes(), b"keep staging notes")
        self.assertEqual(also_unrelated.read_bytes(), b"keep shop notes")

    def test_first_upload_does_not_commit_if_a_new_parent_cannot_be_synced(self):
        directories = []
        state = self.db.parent
        sync = attachment_domain.blobs.sync_directory

        def fail_sync(path):
            directories.append(path)
            if path == state:
                raise OSError("Synthetic state directory sync failure")
            sync(path)

        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair_id = self.repair(client, auth)["id"]
            with patch.object(attachment_domain.blobs, "sync_directory", fail_sync):
                response = self.upload(client, auth, repair_id, ORIGINAL)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(directories, [state / "files" / "shop-1", state / "files", state])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM attachments"), [(0,)])
            self.assertEqual(self.stored_files(), [])

    def test_populated_schema_nine_upgrade_and_failed_migration_rollback(self):
        restored = self.workspace.parent / "restore"
        (restored / "state").mkdir(parents=True)
        copy = restored / "state" / self.db.name
        initialization.initialize_database(copy, "Previous shop", "UTC", "owner",
                                           PasswordHasher().hash(PASSWORD), target=9)
        with closing(sqlite3.connect(copy)) as db, db:
            db.execute("INSERT INTO repairs(shop_id,reported_issue) VALUES (1,'Prior issue')")
            db.execute("INSERT INTO work_entries(shop_id,repair_id,author_id,author_username,kind,body) "
                       "VALUES (1,1,1,'owner','note','Prior entry')")
        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic migration failure")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(copy)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (9,))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone())
        result = subprocess.run([*COMMAND, "migrate", "--workspace", str(restored)],
                                env=python_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT body FROM work_entries").fetchall(), [("Prior entry",)])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (initialization.SCHEMA_VERSION,))


if __name__ == "__main__":
    unittest.main()
