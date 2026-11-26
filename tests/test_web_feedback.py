"""UI-2 web feedback gates: notices, field errors and deliberate conflict review."""

from contextlib import closing
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.contracts import StageChange
from repairlib.domain import repairs as repair_domain
from repairlib.domain.repairs import RepairAccess, RepairError
from repairlib.domain.setup import initialize_workspace
from test_charges import PART
from test_repairs import API, DEVICE
from test_tokens import PASSWORD, TokenFixture, hidden, issued_secret
from test_users import ACCOUNT
from test_users import create_form


def form_for(page, action, *, reviewed=None):
    forms = re.findall(
        rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', page.text, re.S
    )
    for html in forms:
        marked = 'name="reviewed" value="1"' in html
        if reviewed is None or marked == reviewed:
            return dict(re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html
            ))
    raise AssertionError(f"Missing form for {action} (reviewed={reviewed})")


class WebFeedbackTests(TokenFixture, unittest.TestCase):
    def test_unknown_page_shows_an_error_without_a_visible_page_title(self):
        with self.client() as client:
            self.sign_in(client)
            response = client.get("/not-a-real-page")
            self.assertEqual(response.status_code, 404)
            self.assertIn('id="error-message">Page not found</div>', response.text)
            self.assertIn('<h1 class="rl-page-title rl-sr-only">Page not found</h1>', response.text)

    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def create(self, client, auth):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex}, json=DEVICE)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def stage_form(self, page, repair_id):
        return form_for(page, f"/repairs/{repair_id}/stage")

    def test_stage_success_notice_known_codes_only_and_field_errors(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.create(client, auth)
            detail = f"/repairs/{repair['id']}"
            self.assertIn("Repair status updated.", client.get(detail + "?saved=stage").text)
            unknown = client.get(detail + "?saved=not-a-code")
            self.assertNotIn('role="status"', unknown.text)
            greek = client.get(detail + "?saved=stage", headers={"Accept-Language": "el"})
            self.assertIn("Η κατάσταση επισκευής ενημερώθηκε.", greek.text)

            self.assertEqual(client.post(
                f"{API}/{repair['id']}/stage",
                headers={**auth, "Idempotency-Key": uuid4().hex},
                json={"revision": 1, "stage": "in_progress", "reason": ""},
            ).status_code, 200)
            self.assertEqual(client.post(
                f"{API}/{repair['id']}/stage",
                headers={**auth, "Idempotency-Key": uuid4().hex},
                json={"revision": 2, "stage": "repaired", "reason": ""},
            ).status_code, 200)
            form = {**self.stage_form(client.get(detail), repair["id"]),
                    "revision": "3", "stage": "in_progress", "reason": "x" * 1001}
            refused = client.post(f"{detail}/stage", data=form)
            self.assertEqual(refused.status_code, 422)
            self.assertIn("Use at most 1000 printable characters for the reason.", refused.text)
            self.assertNotIn('<textarea id="reason"', refused.text)
            self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                           (repair["id"],)), [("repaired", 3)])
            self.assertEqual(len(self.snapshot(
                "SELECT operation FROM audit_events WHERE operation='repair.stage'"
            )), 2)

    def test_success_notice_is_a_dismissible_self_closing_toast(self):
        with self.client() as client:
            _, auth = self.admin_auth(client)
            repair = self.create(client, auth)
            html = client.get(f"/repairs/{repair['id']}?saved=stage").text
            toast = re.search(r'<details class="obi-toast"[^>]*>(.*?)</details>', html, re.S)
            self.assertIsNotNone(toast, html)
            self.assertIn("open", toast.group(0).split(">", 1)[0])
            self.assertIn('role="status"', toast.group(1))
            self.assertIn("Repair status updated.", toast.group(1))
            self.assertIn("<summary", toast.group(1))
            self.assertIn("Dismiss", toast.group(1))
            self.assertNotIn("<form", toast.group(1))
            archived = client.get(f"/repairs/{repair['id']}?saved=not-a-code").text
            self.assertNotIn('class="obi-toast"', archived)

    def test_stale_stage_shows_saved_and_draft_and_requires_review(self):
        with self.client() as first, self.client() as second:
            _, auth = self.admin_auth(first)
            self.sign_in(second)
            repair = self.create(first, auth)
            detail = f"/repairs/{repair['id']}"
            first_form = {**self.stage_form(first.get(detail), repair["id"]),
                          "stage": "in_progress"}
            second_form = {**self.stage_form(second.get(detail), repair["id"]),
                           "stage": "no_repair"}
            self.assertEqual(first.post(f"{detail}/stage", data=first_form).status_code, 303)
            self.assertEqual(first.post(f"{detail}/stage", data={**first_form, "key": uuid4().hex,
                                                                  "revision": "2",
                                                                  "stage": "repaired"}).status_code, 303)
            before = self.snapshot("SELECT stage,revision FROM repairs WHERE id=?", (repair["id"],))
            self.assertEqual(before, [("repaired", 3)])
            audits = self.snapshot("SELECT id FROM audit_events WHERE operation='repair.stage'")
            requests = self.snapshot("SELECT COUNT(*) FROM repair_requests")

            stale = second.post(f"{detail}/stage", data=second_form)
            self.assertEqual(stale.status_code, 409)
            self.assertIn("Repaired", stale.text)
            self.assertIn("No Repair", stale.text)
            self.assertNotIn('id="stage-form"', stale.text)
            self.assertRegex(stale.text, r'name="review_revision" value="3"')
            self.assertIn('name="reviewed" value="1"', stale.text)
            review = form_for(stale, f"{detail}/stage", reviewed=True)
            self.assertEqual(review["review_revision"], "3")
            self.assertNotIn('<option value="no_repair" selected>', stale.text)
            self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                           (repair["id"],)), before)
            self.assertEqual(self.snapshot("SELECT id FROM audit_events WHERE operation='repair.stage'"),
                             audits)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM repair_requests"), requests)

            replayed_stale = second.post(f"{detail}/stage", data=second_form)
            self.assertEqual(replayed_stale.status_code, 409)
            reviewed = second.post(f"{detail}/stage", data={
                **review, "stage": "in_progress",
            })
            self.assertEqual(reviewed.status_code, 303, reviewed.text)
            self.assertEqual(reviewed.headers["location"], detail + "?saved=stage")
            self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                           (repair["id"],)), [("in_progress", 4)])
            replay = second.post(f"{detail}/stage", data={
                **review, "stage": "in_progress",
            })
            self.assertEqual(replay.status_code, 303)
            self.assertEqual(len(self.snapshot("SELECT id FROM audit_events WHERE operation='repair.stage'")),
                             len(audits) + 1)

    def test_record_status_timezone_archive_confirmation_and_permissions(self):
        with self.client() as owner:
            _, auth = self.admin_auth(owner)
            repair = self.create(owner, auth)
            detail = f"/repairs/{repair['id']}"
            with closing(sqlite3.connect(self.db)) as db:
                with db:
                    db.execute("UPDATE shop_settings SET timezone='Asia/Tokyo' WHERE shop_id=1")
                created = db.execute("SELECT created_at FROM repairs WHERE id=?",
                                     (repair["id"],)).fetchone()[0]
            expected = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(
                ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
            page = owner.get(detail)
            self.assertIn(f'<time datetime="{created}">{expected}</time>', page.text)
            self.assertIn('<option value="" selected disabled hidden>New</option>', page.text)
            self.assertIn('<option value="" selected disabled hidden>Not picked up</option>', page.text)
            self.assertNotIn(f'action="{detail}/archive"', page.text)
            self.assertNotIn("Back to repairs", page.text)
            archived = owner.post(f"{API}/{repair['id']}/archive",
                                  headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"revision": 1, "enabled": True})
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertIsNotNone(self.snapshot("SELECT archived_at FROM repairs WHERE id=?",
                                               (repair["id"],))[0][0])
            self.assertIn("Archived", owner.get(detail).text)

            self.assertEqual(owner.post("/users/new", data=create_form(
                owner, username="viewer", role="viewer", password="viewer passphrase",
            )).status_code, 303)
        with self.client() as viewer:
            login_page = viewer.get("/login")
            signed_in = viewer.post("/login", data={
                "username": "viewer", "password": "viewer passphrase",
                "csrf": hidden(login_page, "csrf"),
            })
            self.assertEqual(signed_in.status_code, 303)
            readable = viewer.get(detail)
            self.assertEqual(readable.status_code, 200)
            self.assertIn(DEVICE["reported_issue"], readable.text)
            self.assertNotIn(f'action="{detail}/stage"', readable.text)
            self.assertNotIn(f'action="{detail}/discount"', readable.text)
            self.assertNotIn(f'action="{detail}/archive"', readable.text)
            with self.assertRaises(RepairError) as refused:
                repair_domain.change_stage(
                    self.workspace, RepairAccess("web", viewer.cookies.get("repairlib_session", "")),
                    repair["id"], StageChange(revision=2, stage="in_progress", reason=""),
                    key=uuid4().hex,
                )
            self.assertEqual(refused.exception.status, 403)
            self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                           (repair["id"],)), [("new", 2)])

        with TemporaryDirectory() as temp:
            elsewhere = Path(temp) / "workspace"
            initialize_workspace(elsewhere, shop="Other", timezone="UTC",
                                 username="owner", password=PASSWORD)
            with TestClient(create_app(workspace=elsewhere), base_url="http://127.0.0.1",
                            follow_redirects=False) as stranger:
                self.sign_in(stranger)
                self.assertEqual(stranger.get(detail).status_code, 404)
                token = issued_secret(stranger.post(ACCOUNT, data=self.issue_form(stranger)))
                auth = {"Authorization": f"Bearer {token}"}
                local = stranger.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                                      json=DEVICE).json()
                form = self.stage_form(stranger.get(f"/repairs/{local['id']}"), local["id"])
                with closing(sqlite3.connect(elsewhere / "state" / "repairlib.sqlite3")) as db:
                    with db:
                        db.execute("DELETE FROM repair_requests WHERE repair_id=?", (local["id"],))
                        db.execute("DELETE FROM repairs WHERE id=?", (local["id"],))
                self.assertEqual(stranger.post(f"{detail}/stage", data={
                    **form, "revision": "1", "stage": "in_progress", "reason": "",
                }).status_code, 404)
        self.assertEqual(self.snapshot("SELECT stage,revision FROM repairs WHERE id=?",
                                       (repair["id"],)), [("new", 2)])


if __name__ == "__main__":
    unittest.main()
