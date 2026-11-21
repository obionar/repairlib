"""I18N-1 gates: Greek refusals, translated HTML error pages, stage labels and per-language dates."""

import ast
from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from repairlib import i18n
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, hidden, issued_secret

SRC = Path(__file__).resolve().parents[1] / "src" / "repairlib"
GREEK = {"Cookie": f"{i18n.COOKIE}=el"}
EL = i18n.CATALOGS["el"]["messages"]
STAGES = ("New", "In Progress", "Waiting Parts", "Lab Test", "Customer Test", "Repaired", "No Repair")
NOT_ERRORS = {"HTTPException", "ValueError", "OSError", "TypeError", "RuntimeError", "KeyError",
              "WorkspaceError", "ClientError", "ArchiveError", "SnapshotError", "UpgradeError"}


def message_key(node):
    """The catalog key for a literal, or for an f-string with `{name}` placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(part.value)
            else:
                expression = part.value
                parts.append("{" + (expression.attr if isinstance(expression, ast.Attribute)
                                    else expression.id) + "}")
        return "".join(parts)
    return None


def reachable_messages():
    """Messages a signed-in browser can be refused with, read from the shared source."""
    found = set()
    files = [*(SRC / "domain").glob("*.py"), *(SRC / "adapters" / "web").glob("*.py"),
             SRC / "money.py", SRC / "search.py"]
    for path in files:
        if path.name == "portable_shop.py":
            continue  # reachable through the API and CLI only
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            candidates = []
            if name.endswith("Error") and name not in NOT_ERRORS and len(node.args) >= 3:
                candidates.append(node.args[2])
                if len(node.args) >= 4 and isinstance(node.args[3], ast.Dict):
                    candidates.extend(node.args[3].values)
            elif name == "HTTPException" and len(node.args) == 2:
                candidates.append(node.args[1])
            elif name == "ValueError" and path.name in {"money.py", "search.py"} and node.args:
                candidates.append(node.args[0])
            elif name == "invalid" and len(node.args) == 2:
                candidates.append(node.args[1])
            for keyword in node.keywords:
                if keyword.arg in {"capability", "resource"}:
                    candidates.append(keyword.value)
            for candidate in candidates:
                key = message_key(candidate)
                if key is not None:
                    found.add(key)
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "message"
                                                     for t in node.targets)
                    and isinstance(node.value, ast.IfExp)):
                found.update(message_key(branch) for branch in (node.value.body, node.value.orelse))
    return found | {"manage users", "customers"}  # defaults of the capability and resource parameters


class CatalogGateTests(unittest.TestCase):
    def test_every_reachable_domain_message_has_greek(self):
        messages = reachable_messages()
        self.assertGreater(len(messages), 200)
        english = json.loads((SRC / "locales" / "en.json").read_text())["messages"]
        missing = sorted(key for key in messages if key not in english or not EL.get(key, "").strip())
        self.assertEqual(missing, [], "reachable messages without English and Greek entries")
        untranslated = sorted(key for key in messages if EL[key] == key)
        self.assertEqual(untranslated, [])

    def test_parameterized_messages_translate_after_formatting(self):
        gettext = i18n.translator("el")
        shown = gettext("Enter a whole number of MiB from 1 to 2048.")
        self.assertNotEqual(shown, "Enter a whole number of MiB from 1 to 2048.")
        self.assertIn("2048", shown)
        self.assertEqual(i18n.translator("en")("Enter a whole number of MiB from 1 to 2048."),
                         "Enter a whole number of MiB from 1 to 2048.")

    def test_templates_use_the_stage_label_helper(self):
        for path in (SRC / "adapters" / "web" / "templates").glob("*.html"):
            self.assertNotIn("replace('_'", path.read_text(), path.name)


class WebFixture(TokenFixture):
    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def greek(self, client):
        client.cookies.set(i18n.COOKIE, "el")

    def assert_error_page(self, page, status, heading):
        self.assertEqual(page.status_code, status, page.text[:300])
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn('<html lang="el">', page.text)
        self.assertIn(EL[heading], page.text)
        self.assertIn(EL["Go back"], page.text)
        self.assertNotIn(heading, page.text)
        self.assertNotIn('"detail"', page.text)


class StageLabelTests(WebFixture, unittest.TestCase):
    def visible_stage_labels(self, html):
        labels = re.findall(r'class="obi-status"[^>]*>([^<]+)<', html)
        labels += re.findall(r"<dt>[^<]*</dt><dd>([^<]+)</dd>", html)
        labels += re.findall(r"<option[^>]*>([^<]+)</option>", html)
        labels += re.findall(r"<span>([^<]+)</span>", html)
        return [label.strip() for label in labels if label.strip() in STAGES]

    def test_no_english_stage_label_on_detail_list_conflict_or_print(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.greek(client)
            repair = self.repair(client, auth)
            repair_id = repair["id"]
            for stage in ("waiting_parts", "customer_test", "no_repair"):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("UPDATE repairs SET stage = ? WHERE id = ?", (stage, repair_id))
                for path in (f"/repairs/{repair_id}", "/repairs", "/", f"/repairs/{repair_id}/print"):
                    page = client.get(path)
                    self.assertEqual(page.status_code, 200, path)
                    self.assertEqual(self.visible_stage_labels(page.text), [], f"{stage} {path}")
                    self.assertIn(EL[stage.replace("_", " ").title()], page.text, f"{stage} {path}")
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE repairs SET stage = 'in_progress', revision = 3 WHERE id = ?", (repair_id,))
            detail = client.get(f"/repairs/{repair_id}").text
            stale = {"key": uuid4().hex, "revision": "1", "stage": "lab_test", "reason": ""}
            form = re.search(rf'<form method="post" action="/repairs/{repair_id}/stage".*?</form>',
                             detail, re.S).group()
            stale["csrf"] = re.search(r'name="csrf" value="([^"]+)"', form)[1]
            conflict = client.post(f"/repairs/{repair_id}/stage", data=stale)
            self.assertEqual(conflict.status_code, 409)
            self.assertIn(EL["Proposed status"], conflict.text)
            review = conflict.text.split('id="review-stage"', 1)[1].split('</select>', 1)[0]
            self.assertEqual(re.findall(r'<option value="([a-z_]+)"', review),
                             ["new", "in_progress", "waiting_parts", "lab_test", "customer_test",
                              "repaired", "no_repair"])
            self.assertIn('<option value="lab_test" selected>', review)
            self.assertIn('<option value="new" disabled>', review)
            self.assertEqual(self.visible_stage_labels(conflict.text), [])


class RefusalTests(WebFixture, unittest.TestCase):
    def test_settings_refusals_render_in_greek(self):
        with self.client() as client:
            self.admin_auth(client)
            self.greek(client)
            page = client.get("/settings")
            form = {name: hidden(page, name) for name in ("csrf", "key", "revision", "name", "timezone")}
            refused = client.post("/settings", data={**form, "timezone": "Not/A_Zone", "max_upload_mib": "0",
                                                     "inline_preview_mib": "8"})
            self.assertEqual(refused.status_code, 422)
            for english in ("Check the shop settings.", "Enter a valid IANA timezone."):
                self.assertNotIn(english, refused.text)
                self.assertIn(EL[english], refused.text)
            self.assertNotIn("Enter a whole number of MiB", refused.text)
            self.assertIn("2048", refused.text)


class ErrorPageTests(WebFixture, unittest.TestCase):
    def test_unknown_page_is_html_for_browsers_and_json_for_the_api(self):
        with self.client() as client:
            token, auth = self.admin_auth(client)
            self.greek(client)
            self.assert_error_page(client.get("/no-such-page"), 404, "Page not found")
            api = client.get("/api/v1/no-such-route", headers=auth)
            self.assertEqual(api.status_code, 404)
            self.assertIn("application/json", api.headers["content-type"])
            conflict = client.post(f"{API}/999/stage", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 1, "stage": "repaired", "reason": ""})
            self.assertIn("application/json", conflict.headers["content-type"])
            self.assertEqual(conflict.json()["error"]["code"], "not_found")
        with self.client() as anonymous:
            anonymous.cookies.set(i18n.COOKIE, "el")
            self.assert_error_page(anonymous.get("/no-such-page"), 404, "Page not found")

    def test_refused_pages_use_the_shared_error_page(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.greek(client)
            self.assert_error_page(client.get("/repairs/99999"), 404, "Page not found")
            repair = self.repair(client, auth)
            archived = client.post(f"{API}/{repair['id']}/archive", headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": 1, "enabled": True})
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assert_error_page(client.get(f"/repairs/{repair['id']}/consume-part"), 409, "Conflict")
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO users(shop_id,username,role,password_hash) "
                           "SELECT shop_id,'viewer','viewer',password_hash FROM users WHERE id=1")
        with self.client() as viewer:
            self.sign_in(viewer, "viewer")
            viewer.cookies.set(i18n.COOKIE, "el")
            self.assert_error_page(viewer.get("/settings"), 403, "Access denied")

    def test_purged_attachment_is_a_gone_page(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            self.greek(client)
            repair = self.repair(client, auth)
            created = client.post(f"{API}/{repair['id']}/attachments", params={"filename": "a.bin"},
                                  headers={**auth, "Idempotency-Key": uuid4().hex,
                                           "Content-Type": "application/octet-stream"}, content=b"bytes")
            self.assertEqual(created.status_code, 201, created.text)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE attachments SET detached_at = '2026-01-01T00:00:00.000Z', "
                           "purged_at = '2026-03-01T00:00:00.000Z', purged_by_username = 'owner'")
            page = client.get(f"/repairs/{repair['id']}/attachments/{created.json()['id']}/content")
            self.assert_error_page(page, 410, "No longer available")

    def test_server_failure_and_busy_workspace_pages_are_translated(self):
        with self.client() as client:
            self.admin_auth(client)
            self.greek(client)
            with patch("repairlib.adapters.web.routes.read_dashboard", side_effect=RuntimeError("boom")):
                failed = client.get("/")
            self.assert_error_page(failed, 500, "Something went wrong")
            self.assertNotIn("boom", failed.text)
            self.assertIn(failed.headers["x-request-id"], failed.text)
            busy_error = sqlite3.OperationalError("database is locked")
            with patch("repairlib.adapters.web.routes.read_dashboard", side_effect=busy_error):
                busy = client.get("/")
            self.assert_error_page(busy, 503, "Temporarily unavailable")

    def test_expired_form_keeps_typed_text_but_never_secrets(self):
        with self.client() as client:
            self.admin_auth(client)
            self.greek(client)
            expired = client.post("/settings", data={
                "csrf": "stale", "key": uuid4().hex, "revision": "1", "name": "Εργαστήριο Δοκιμής",
                "timezone": "Europe/Athens", "password": "hunter2-secret",
            })
            self.assert_error_page(expired, 403, "Access denied")
            self.assertIn(EL["Form expired. Open the page again and retry."], expired.text)
            self.assertIn("Εργαστήριο Δοκιμής", expired.text)
            self.assertIn("Europe/Athens", expired.text)
            self.assertNotIn("hunter2-secret", expired.text)
            self.assertNotIn('value="stale"', expired.text)
        with self.client() as signed_out:
            signed_out.cookies.set(i18n.COOKIE, "el")
            lost = signed_out.post("/settings", data={"csrf": "x", "name": "Χαμένο κείμενο"})
            self.assertEqual(lost.status_code, 403)
            self.assertIn("Χαμένο κείμενο", lost.text)


class BackLinkTests(WebFixture, unittest.TestCase):
    def test_error_page_back_link_never_leaves_the_origin(self):
        with self.client() as client:
            client.get("/")  # first request sets the session cookie
            for referer in (r"https://evil.example/\evil.example",
                            r"http://127.0.0.1/\evil.example",
                            "http://127.0.0.1//evil.example"):
                page = client.get("/no-such-page", headers={"Referer": referer})
                self.assertEqual(page.status_code, 404)
                self.assertNotIn("evil.example", page.text)
                self.assertNotIn("/\\", page.text)
            same = client.get("/no-such-page", headers={"Referer": "http://127.0.0.1/repairs?q=phone"})
            self.assertIn('href="/repairs?q=phone"', same.text)


class DateFormatTests(WebFixture, unittest.TestCase):
    def test_queue_dates_follow_the_language_without_time_or_age(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            moment = datetime.now(UTC) - timedelta(days=3, hours=1)
            stamp = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE repairs SET created_at = ? WHERE id = ?", (stamp, repair["id"]))
            english = client.get("/")
            self.assertIn(f'<time datetime="{stamp}">{moment:%Y-%m-%d}</time>', english.text)
            self.assertNotIn("3 d ago", english.text)
            self.greek(client)
            greek = client.get("/")
            self.assertIn(f'<time datetime="{stamp}">{moment:%d/%m/%Y}</time>', greek.text)
            self.assertNotIn(EL["{count} d ago"].format(count=3), greek.text)
            self.assertNotIn("3 d ago", greek.text)


if __name__ == "__main__":
    unittest.main()
