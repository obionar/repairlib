"""A2/W8: a retried write replays its stored result even after the record moved on.

Replay needs only (shop, actor, operation, key, fingerprint), so it must run before
any current-state guard such as archived or deleted. Different payloads on the same
key stay a conflict, and a fresh key still meets the guard.
"""

import unittest
from uuid import uuid4

from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret


class ReplayAfterStateTests(TokenFixture, unittest.TestCase):
    def auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def headers(auth, key=None):
        return {**auth, "Idempotency-Key": key or uuid4().hex}

    def archive(self, client, auth, repair_id, revision):
        response = client.post(f"{API}/{repair_id}/archive", headers=self.headers(auth),
                               json={"revision": revision, "enabled": True})
        self.assertEqual(response.status_code, 200, response.text)

    def assert_replays(self, client, send, first, *, fresh_status):
        """`send(key)` repeats the request; it must replay `first` on the old key only."""
        key, original = first
        replay = send(key)
        self.assertEqual((replay.status_code, replay.json()), (original.status_code, original.json()),
                         replay.text)
        self.assertEqual(send(uuid4().hex).status_code, fresh_status)

    def test_repair_update_stage_and_entry_create_replay_after_archive(self):
        with self.client() as client:
            auth = self.auth(client)
            repair = client.post(API, headers=self.headers(auth), json=DEVICE).json()
            rid = repair["id"]
            update_key, stage_key, entry_key = (uuid4().hex for _ in range(3))
            update_body = {"revision": 1, "reported_issue": "Replayed synthetic edit"}
            updated = client.patch(f"{API}/{rid}", headers=self.headers(auth, update_key), json=update_body)
            self.assertEqual(updated.status_code, 200, updated.text)
            stage_body = {"revision": 2, "stage": "in_progress", "reason": ""}
            staged = client.post(f"{API}/{rid}/stage", headers=self.headers(auth, stage_key), json=stage_body)
            self.assertEqual(staged.status_code, 200, staged.text)
            entry_body = {"kind": "work", "body": "Replayed synthetic entry"}
            entry = client.post(f"{API}/{rid}/entries", headers=self.headers(auth, entry_key), json=entry_body)
            self.assertEqual(entry.status_code, 201, entry.text)
            self.archive(client, auth, rid, 3)
            self.assert_replays(
                client, lambda key: client.patch(f"{API}/{rid}", headers=self.headers(auth, key), json=update_body),
                (update_key, updated), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.post(f"{API}/{rid}/stage", headers=self.headers(auth, key), json=stage_body),
                (stage_key, staged), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.post(f"{API}/{rid}/entries", headers=self.headers(auth, key), json=entry_body),
                (entry_key, entry), fresh_status=409)
            conflict = client.post(f"{API}/{rid}/entries", headers=self.headers(auth, entry_key),
                                   json={**entry_body, "body": "Different"})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["error"]["code"] if "error" in conflict.json() else "conflict",
                             "conflict")

    def test_entry_correction_and_visibility_replay_after_delete_and_archive(self):
        with self.client() as client:
            auth = self.auth(client)
            repair = client.post(API, headers=self.headers(auth), json=DEVICE).json()
            rid = repair["id"]
            entry = client.post(f"{API}/{rid}/entries", headers=self.headers(auth),
                                json={"kind": "work", "body": "Original synthetic entry"}).json()
            url = f"{API}/{rid}/entries/{entry['id']}"
            correct_key, hide_key = uuid4().hex, uuid4().hex
            correct_body = {"revision": 1, "kind": "note", "body": "Corrected", "reason": "Clarified"}
            corrected = client.patch(url, headers=self.headers(auth, correct_key), json=correct_body)
            self.assertEqual(corrected.status_code, 200, corrected.text)
            hide_body = {"revision": 2, "deleted": True, "reason": "Duplicate"}
            hidden_entry = client.post(f"{url}/visibility", headers=self.headers(auth, hide_key), json=hide_body)
            self.assertEqual(hidden_entry.status_code, 200, hidden_entry.text)
            # The entry is now deleted: the original correction must still replay.
            self.assert_replays(
                client, lambda key: client.patch(url, headers=self.headers(auth, key), json=correct_body),
                (correct_key, corrected), fresh_status=409)
            self.archive(client, auth, rid, 1)
            self.assert_replays(
                client, lambda key: client.post(f"{url}/visibility", headers=self.headers(auth, key), json=hide_body),
                (hide_key, hidden_entry), fresh_status=409)

    def test_charge_and_discount_replay_after_archive(self):
        part = {"kind": "part", "description": "Synthetic screen", "quantity_milli": 1000,
                "unit": "pc", "unit_price_cents": 2999, "unit_cost_cents": 800, "notes": ""}
        with self.client() as client:
            auth = self.auth(client)
            rid = client.post(API, headers=self.headers(auth), json=DEVICE).json()["id"]
            create_key, correct_key, hide_key, discount_key = (uuid4().hex for _ in range(4))
            created = client.post(f"{API}/{rid}/charges", headers=self.headers(auth, create_key), json=part)
            self.assertEqual(created.status_code, 201, created.text)
            url = f"{API}/{rid}/charges/{created.json()['id']}"
            correct_body = {**part, "revision": 1, "unit_price_cents": 3100, "reason": "Price check"}
            corrected = client.patch(url, headers=self.headers(auth, correct_key), json=correct_body)
            self.assertEqual(corrected.status_code, 200, corrected.text)
            hide_body = {"revision": 2, "deleted": True, "reason": "Wrong part"}
            hidden = client.post(f"{url}/visibility", headers=self.headers(auth, hide_key), json=hide_body)
            self.assertEqual(hidden.status_code, 200, hidden.text)
            restored = client.post(f"{url}/visibility", headers=self.headers(auth),
                                   json={"revision": 3, "deleted": False, "reason": "Right part"})
            self.assertEqual(restored.status_code, 200, restored.text)
            discount_body = {"revision": 0, "kind": "percent", "value": 10}
            discount = client.put(f"{API}/{rid}/discount", headers=self.headers(auth, discount_key),
                                  json=discount_body)
            self.assertEqual(discount.status_code, 200, discount.text)
            self.archive(client, auth, rid, 1)
            self.assert_replays(
                client, lambda key: client.post(f"{API}/{rid}/charges", headers=self.headers(auth, key), json=part),
                (create_key, created), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.patch(url, headers=self.headers(auth, key), json=correct_body),
                (correct_key, corrected), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.post(f"{url}/visibility", headers=self.headers(auth, key), json=hide_body),
                (hide_key, hidden), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.put(f"{API}/{rid}/discount", headers=self.headers(auth, key),
                                               json=discount_body),
                (discount_key, discount), fresh_status=409)

    def test_attachment_caption_and_detach_replay_after_archive(self):
        content = b"synthetic attachment bytes"
        with self.client() as client:
            auth = self.auth(client)
            rid = client.post(API, headers=self.headers(auth), json=DEVICE).json()["id"]

            def upload(key):
                return client.post(f"{API}/{rid}/attachments", params={"filename": "report.bin"},
                                   headers={**self.headers(auth, key),
                                            "Content-Type": "application/octet-stream"},
                                   content=content)

            caption_key, detach_key = uuid4().hex, uuid4().hex
            created = upload(uuid4().hex)
            self.assertEqual(created.status_code, 201, created.text)
            url = f"{API}/{rid}/attachments/{created.json()['id']}"
            caption_body = {"revision": 1, "caption": "Board"}
            captioned = client.patch(url, headers=self.headers(auth, caption_key), json=caption_body)
            self.assertEqual(captioned.status_code, 200, captioned.text)
            detach_body = {"revision": 2, "detached": True}
            detached = client.post(f"{url}/detach", headers=self.headers(auth, detach_key), json=detach_body)
            self.assertEqual(detached.status_code, 200, detached.text)
            self.archive(client, auth, rid, 1)
            # Uploads stay refused before any bytes are staged (prepare_upload); only the
            # metadata writes below can be replayed once the repair is archived.
            # The caption was set while attached; the attachment is now detached, so only
            # replay can answer the original caption key.
            self.assert_replays(
                client, lambda key: client.patch(url, headers=self.headers(auth, key), json=caption_body),
                (caption_key, captioned), fresh_status=409)
            self.assert_replays(
                client, lambda key: client.post(f"{url}/detach", headers=self.headers(auth, key), json=detach_body),
                (detach_key, detached), fresh_status=409)


if __name__ == "__main__":
    unittest.main()
