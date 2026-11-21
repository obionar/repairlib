"""Pickup review and audited status reversal through the browser."""

import re
import unittest
from uuid import uuid4

from repairlib.adapters.web.security import SESSION_COOKIE, csrf_token
from repairlib.contracts import RepairInput
from repairlib.lifecycle.workspace import load_form_key
from repairlib.storage import repairs as repair_storage
from repairlib.storage.customers import transaction
from repairlib.storage.initialization import seed_workspace
from test_charges import LABOR, PART
from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, PASSWORD, TokenFixture, issued_secret
from test_users import create_form
from test_web_feedback import form_for
from test_web_repair_workspace import panel


class PickupHandoverTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def start_repair(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        auth = {"Authorization": f"Bearer {token}"}
        repair_id = self.write(client, auth, API, DEVICE)["id"]
        return auth, f"{API}/{repair_id}", f"/repairs/{repair_id}"

    def write(self, client, auth, url, payload, *, status=201):
        reply = client.post(url, json=payload, headers={**auth, "Idempotency-Key": uuid4().hex})
        self.assertEqual(reply.status_code, status, reply.text)
        return reply.json()

    def stage_csrf(self, client, repair_id):
        return csrf_token(load_form_key(self.workspace), f"repair.stage:{repair_id}",
                          client.cookies[SESSION_COOKIE])

    def test_pickup_and_payment_are_disabled_until_finished_and_save_atomically(self):
        with self.client() as client:
            auth, api, detail = self.start_repair(client)
            page = client.get(detail)
            status = panel(page, "status")
            for control in ("pickup", "payment"):
                self.assertRegex(status, rf'<select id="{control}-state"[^>]*\bdisabled>')
            self.assertEqual(status.count('>Update status</button>'), 1)
            self.assertNotIn("More actions", status)
            action = detail + "/stage"
            data = {"pickup": "1", "payment": "1"}
            before = client.get(api, headers=auth).json()
            refused = client.post(action, data=form_for(page, action) | data | {"stage": "no_repair"})
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(client.get(api, headers=auth).json(), before)
            self.write(client, auth, api + "/stage", {"revision": 1, "stage": "no_repair"}, status=200)
            data |= form_for(client.get(detail), action)
            self.assertEqual(client.post(action, data=data | {"payment": "bad"}).status_code, 422)
            self.assertIsNone(client.get(api, headers=auth).json()["picked_up_at"])
            self.assertEqual(client.post(action, data=data).status_code, 303)
            after = client.get(api, headers=auth).json()
            self.assertIsNotNone(after["picked_up_at"])
            self.assertEqual(client.post(action, data=data).status_code, 303)
            self.assertEqual(client.get(api, headers=auth).json(), after)
            self.assertIn('selected disabled hidden>Paid</option>', client.get(detail).text)
            events = client.get(api + "/activity", headers=auth).json()["items"]
            self.assertEqual([e["operation"] for e in events[:2]], ["repair.payment", "repair.pickup"])
            fields = form_for(client.get(detail), action)
            self.assertEqual(client.post(action, data=fields | {"payment": "0"}).status_code, 303)
            self.assertIn('selected disabled hidden>Not paid</option>', client.get(detail).text)

    def test_hand_over_marks_paid_and_picked_up_in_one_step(self):
        with self.client() as client:
            auth, api, detail = self.start_repair(client)
            self.assertNotIn(">Hand over</button>", panel(client.get(detail), "status"))
            self.write(client, auth, api + "/stage", {"revision": 1, "stage": "no_repair"}, status=200)
            status = panel(client.get(detail), "status")
            form = re.search(r'<form[^>]*id="handover-form"[^>]*>(.*?)</form>', status, re.S)
            self.assertIsNotNone(form)
            self.assertIn(">Hand over</button>", form.group(1))
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form.group(1)))
            self.assertEqual((fields["pickup"], fields["payment"], fields.get("stage", "")), ("1", "1", ""))
            before = client.get(api, headers=auth).json()
            self.assertEqual(client.post(detail + "/stage", data=fields | {"revision": "1"}).status_code, 409)
            self.assertEqual(client.get(api, headers=auth).json(), before)
            saved = client.post(detail + "/stage", data=fields)
            self.assertEqual(saved.status_code, 303)
            self.assertIn("saved=handover", saved.headers["location"])
            self.assertEqual(client.post(detail + "/stage", data=fields).status_code, 303)  # Same key replays.
            after = client.get(api, headers=auth).json()
            self.assertIsNotNone(after["picked_up_at"])
            page = client.get(saved.headers["location"])
            status = panel(page, "status")
            self.assertIn("Handed over: paid and picked up.", status)
            self.assertNotIn('id="handover-form"', status)
            self.assertRegex(status, r'<option value=""[^>]*>Paid</option>')
            self.assertRegex(status, r'<option value=""[^>]*>Picked up</option>')
            operations = [row[0] for row in self.snapshot(
                "SELECT operation FROM audit_events WHERE operation IN ('repair.pickup', 'repair.payment')")]
            self.assertEqual(operations, ["repair.pickup", "repair.payment"])

    def test_reopen_with_pickup_preserves_both_unsaved_choices(self):
        with self.client() as client:
            auth, api, detail = self.start_repair(client)
            saved = self.write(client, auth, api + "/stage", {"revision": 1, "stage": "no_repair"}, status=200)
            action = detail + "/stage"
            response = client.post(action, data=form_for(client.get(detail), action) |
                                   {"stage": "in_progress", "pickup": "1"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("Reopen the repair separately", response.text)
            self.assertIn('<option value="in_progress" selected>', response.text)
            self.assertIn('<option value="1" selected>Picked up</option>', response.text)
            self.assertEqual(client.get(api, headers=auth).json(), saved)

    def test_payment_shows_current_total_without_estimate_wording(self):
        with self.client() as client:
            auth, api, detail = self.start_repair(client)

            def shown_total():
                page = panel(client.get(detail), "payment-total")
                self.assertNotIn("estimate", page.lower())
                cents = client.get(f"{api}/charges", headers=auth).json()["total_cents"]
                return page, cents

            self.write(client, auth, api + "/charges", PART)
            page, cents = shown_total()
            self.assertIn(f"{(cents + 2000) / 100:.2f} EUR", page)  # saved charges plus default labour
            self.write(client, auth, api + "/charges", LABOR)
            page, cents = shown_total()
            self.assertIn(f"{(cents + 2000) / 100:.2f} EUR", page)

    def test_status_toasts_and_dropdown_reversal_replay_once(self):
        with self.client() as client:
            auth, api, detail = self.start_repair(client)
            self.write(client, auth, api + "/stage", {"revision": 1, "stage": "in_progress"}, status=200)
            before = self.write(client, auth, api + "/stage", {"revision": 2, "stage": "repaired"}, status=200)
            status_url = detail + "/stage"
            fields = form_for(client.get(detail), status_url)
            refused = client.post(status_url, data={**fields, "pickup": ""})
            self.assertEqual(refused.status_code, 422)
            self.assertEqual(client.get(api, headers=auth).json(), before)
            confirmed = client.post(status_url, data={**fields, "pickup": "1"})
            self.assertEqual(confirmed.status_code, 303)
            success = client.get(confirmed.headers["location"])
            self.assertIn('id="stage-error" role="alert"', refused.text)
            self.assertIn('<details class="obi-toast" data-tone="error" open>', refused.text)
            status = panel(success, "status")
            self.assertIn('<details class="obi-toast" data-tone="info" open>', status)
            self.assertIn("Pickup state saved.", status)
            self.assertNotIn("Undo pickup", status)
            self.assertEqual(confirmed.headers["location"], detail + "?saved=pickup")
            picked = client.get(api, headers=auth).json()
            self.assertEqual((picked["stage"], picked["revision"]), (before["stage"], before["revision"] + 1))
            self.assertIsNotNone(picked["picked_up_at"])
            undo = {**form_for(success, status_url), "pickup": "0"}
            self.assertEqual(undo["revision"], str(before["revision"] + 1))
            self.assertEqual(client.post(status_url, data=undo).status_code, 303)
            restored = client.get(api, headers=auth).json()
            self.assertEqual((restored["stage"], restored["revision"], restored["picked_up_at"]),
                             (before["stage"], before["revision"] + 2, None))
            self.assertEqual(client.post(status_url, data=undo).status_code, 303)
            self.assertEqual(client.get(api, headers=auth).json(), restored)
            events = client.get(api + "/activity", headers=auth).json()["items"]
            self.assertEqual([(e["operation"], e["source"], e["actor_username"]) for e in events[:3]],
                             [("repair.pickup", "web", "owner"), ("repair.pickup", "web", "owner"),
                              ("repair.stage", "api", "owner")])
            self.assertGreater(events[0]["id"], events[1]["id"])

            picked_again = client.post(status_url, data={**form_for(client.get(detail), status_url), "pickup": "1"})
            self.assertEqual(picked_again.status_code, 303)
            self.assertNotIn("Undo pickup", panel(client.get(confirmed.headers["location"]), "status"))
            self.assertEqual(client.post(status_url, data=undo).status_code, 303)
            current = client.get(api, headers=auth).json()
            self.assertIsNotNone(current["picked_up_at"])
            self.assertEqual(current["revision"], before["revision"] + 3)

    def test_pickup_reversal_refuses_viewer_and_cross_shop_write(self):
        with transaction(self.db, write=True) as db:
            seed_workspace(db, "Other workshop", "UTC", "outsider", "!")
            outside = repair_storage.insert(db, 2, RepairInput(**DEVICE))
            outside = repair_storage.update_flag(db, 2, outside.id, outside.revision, "picked_up_at",
                                                 True, "2026-01-01T00:00:00.000Z")
        with self.client() as owner, self.client() as viewer:
            auth, api, detail = self.start_repair(owner)
            created = owner.post("/users/new", data=create_form(
                owner, username="reader", role="viewer", password=PASSWORD))
            self.assertEqual(created.status_code, 303)
            status_url = detail + "/stage"
            self.write(owner, auth, api + "/stage", {"revision": 1, "stage": "no_repair"}, status=200)
            confirmed = owner.post(status_url, data={**form_for(owner.get(detail), status_url), "pickup": "1"})
            self.assertEqual(confirmed.status_code, 303)
            undo = {**form_for(owner.get(confirmed.headers["location"]), status_url), "pickup": "0"}
            before = owner.get(api, headers=auth).json()
            activity = owner.get(api + "/activity", headers=auth).json()
            self.sign_in(viewer, "reader")
            reply = viewer.post(status_url, data={**undo, "csrf": self.stage_csrf(viewer, before["id"])})
            self.assertEqual(reply.status_code, 403)
            self.assertTrue("Your account or token cannot change " in reply.text,
                            "The viewer should reach the write-permission check")
            self.assertIn('id="pickup-state" name="pickup" form="stage-form" disabled', viewer.get(detail).text)

            foreign = owner.post(f"/repairs/{outside.id}/stage", data={
                **undo, "csrf": self.stage_csrf(owner, outside.id), "revision": str(outside.revision),
            })
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(owner.get(api, headers=auth).json(), before)
            self.assertEqual(owner.get(api + "/activity", headers=auth).json(), activity)
        with transaction(self.db, write=False) as db:
            self.assertEqual(repair_storage.find(db, 2, outside.id), outside)
