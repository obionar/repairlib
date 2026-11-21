"""Agent privacy at the portable-export boundary."""

from unittest import TestCase
from unittest.mock import patch
import sqlite3
from uuid import uuid4

from test_tokens import ACCOUNT, TokenFixture, issued_secret


class AgentPrivacyRegression(TokenFixture, TestCase):
    def test_identity_lost_after_customer_read_masks_response(self):
        with self.client() as client:
            self.sign_in(client)
            issued = client.post(ACCOUNT, data=self.issue_form(client, name="Writer", kind="agent"))
            self.assertEqual(issued.status_code, 201)
            token = issued_secret(issued)
            auth = {"Authorization": f"Bearer {token}"}
            created = client.post("/api/v1/customers", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"name": "Synthetic", "phone": "+357 555 0199"})
            self.assertEqual(created.status_code, 201)
            from repairlib.adapters.api.customers import read_customer

            def revoke_after_read(*args, **kwargs):
                result = read_customer(*args, **kwargs)
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE api_tokens SET revision = revision + 1, "
                               "revoked_at = '2026-11-20T00:00:00Z', revoke_key = ?", (uuid4().hex,))
                return result

            with patch("repairlib.adapters.api.customers.read_customer", side_effect=revoke_after_read):
                response = client.get(f"/api/v1/customers/{created.json()['id']}", headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["phone"], "[masked]")

    def test_revocation_after_customer_create_keeps_success_masked(self):
        with self.client() as client:
            self.sign_in(client)
            issued = client.post(ACCOUNT, data=self.issue_form(client, name="Writer", kind="agent"))
            token = issued_secret(issued)
            from repairlib.adapters.api.customers import create_customer

            def revoke_after_create(*args, **kwargs):
                result = create_customer(*args, **kwargs)
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE api_tokens SET revision = revision + 1, "
                               "revoked_at = '2026-11-20T00:00:00Z', revoke_key = ?", (uuid4().hex,))
                return result

            with patch("repairlib.adapters.api.customers.create_customer", side_effect=revoke_after_create):
                response = client.post("/api/v1/customers", headers={"Authorization": f"Bearer {token}",
                                       "Idempotency-Key": uuid4().hex},
                                       json={"name": "Synthetic", "phone": "+357 555 0199"})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["phone"], "[masked]")
            self.assertEqual(self.snapshot("SELECT count(*) FROM customers"), [(1,)])

    def test_lookup_error_after_customer_create_keeps_success_masked(self):
        with self.client() as client:
            self.sign_in(client)
            issued = client.post(ACCOUNT, data=self.issue_form(client, name="Writer", kind="agent"))
            token = issued_secret(issued)
            with patch("repairlib.adapters.masking.authenticate_api_token",
                       side_effect=sqlite3.OperationalError("synthetic lookup failure")):
                response = client.post("/api/v1/customers", headers={"Authorization": f"Bearer {token}",
                                       "Idempotency-Key": uuid4().hex},
                                       json={"name": "Synthetic", "phone": "+357 555 0199"})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["phone"], "[masked]")
            self.assertEqual(self.snapshot("SELECT count(*) FROM customers"), [(1,)])

    def test_agent_tokens_cannot_export_unmasked_portable_archives(self):
        with self.client() as client:
            self.sign_in(client)
            for preset in ("read-only", "repairs-write", "inventory-write", None):
                form = self.issue_form(client, name=f"Agent {preset}", kind="agent",
                                       key=uuid4().hex, **({"preset": preset} if preset else {}))
                issued = client.post(ACCOUNT, data=form)
                self.assertEqual(issued.status_code, 201)
                token = issued_secret(issued)
                response = client.get("/api/v1/portable/export",
                                      headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 403, preset)

    def test_person_admin_can_still_export_portable_archive(self):
        with self.client() as client:
            self.sign_in(client)
            issued = client.post(ACCOUNT, data=self.issue_form(client, name="Human admin", kind="user"))
            self.assertEqual(issued.status_code, 201)
            token = issued_secret(issued)
            response = client.get("/api/v1/portable/export", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/zip")
