"""UI-7c printed handover: shop header, terms, signature area and the total."""

from contextlib import closing
import json
import sqlite3
import unittest
from uuid import uuid4

from repairlib import i18n
from test_charges import LABOR
from test_repairs import API, CUSTOMERS, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret
from test_web_feedback import form_for

CONTACT = {"address": "Λεωφόρος 1, Λευκωσία", "phone": "+357 22 000 000",
           "terms": "Εγγύηση 90 ημέρες. Τα προϊόντα δεν φυλάσσονται πάνω από 30 ημέρες."}


class PrintHandoverTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def admin_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth):
        customer = client.post(CUSTOMERS, headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"name": "Πελάτης Δοκιμής", "phone": "+357 555 0102"}).json()
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={**DEVICE, "customer_id": customer["id"]})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def store_contact(self, address=CONTACT["address"], phone=CONTACT["phone"],
                      terms=CONTACT["terms"]):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET address = ?, phone = ?, terms = ? WHERE shop_id = 1",
                       (address, phone, terms))

    def test_handover_header_terms_signature_and_total(self):
        self.store_contact()
        with self.client() as client:
            auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            charge = client.post(f"{API}/{repair['id']}/charges",
                                 headers={**auth, "Idempotency-Key": uuid4().hex}, json=LABOR)
            self.assertEqual(charge.status_code, 201, charge.text)
            page = client.get(f"/repairs/{repair['id']}/print").text
            self.assertIn(CONTACT["address"], page)
            self.assertIn(CONTACT["phone"], page)
            self.assertIn(CONTACT["terms"], page)
            self.assertIn("Customer signature", page)
            self.assertNotIn("Accepted estimate", page)

    def test_handover_shows_payment_and_british_labour(self):
        with self.client() as client:
            auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            printed = f"/repairs/{repair['id']}/print"
            client.post(f"{API}/{repair['id']}/charges", headers={**auth, "Idempotency-Key": uuid4().hex}, json=LABOR)
            page = client.get(printed).text
            self.assertNotIn("<dt>Payment</dt>", page)
            self.assertIn("Parts and labour", page)
            self.assertNotIn("Labor", page)
            staged = client.post(f"{API}/{repair['id']}/stage", json={"revision": 1, "stage": "no_repair"},
                                 headers={**auth, "Idempotency-Key": uuid4().hex})
            self.assertEqual(staged.status_code, 200, staged.text)
            ticket = f"/repairs/{repair['id']}"
            paid = client.post(ticket + "/stage", data=form_for(client.get(ticket), ticket + "/stage") | {"payment": "1"})
            self.assertEqual(paid.status_code, 303)
            self.assertIn("<dt>Payment</dt><dd>Paid</dd>", client.get(printed).text)
            client.cookies.set(i18n.COOKIE, "el")
            self.assertIn("<dt>Πληρωμή</dt><dd>Εξοφλημένο</dd>", client.get(printed).text)

    def test_greek_handover_labels_and_absent_contact_block(self):
        self.store_contact(address="", phone="", terms="")
        with self.client() as client:
            auth = self.admin_auth(client)
            repair = self.repair(client, auth)
            client.cookies.set(i18n.COOKIE, "el")
            page = client.get(f"/repairs/{repair['id']}/print").text
            self.assertIn("Υπογραφή πελάτη", page)
            self.assertIn("Ημερομηνία", page)
            self.assertNotIn("print-contact", page)
            self.assertNotIn("print-terms", page)

    def test_settings_form_stores_the_contact_block_with_an_audit_event(self):
        with self.client() as client:
            self.sign_in(client)
            form = client.get("/settings")
            from test_users import hidden
            data = {name: hidden(form, name) for name in ("csrf", "key", "revision", "name", "timezone")}
            data.update({"address": CONTACT["address"], "phone": CONTACT["phone"],
                         "terms": CONTACT["terms"]})
            self.assertEqual(client.post("/settings", data=data).status_code, 303)
        with closing(sqlite3.connect(self.db)) as db:
            row = db.execute("SELECT address, phone, terms FROM shop_settings WHERE shop_id = 1").fetchone()
            changes = db.execute("SELECT changes FROM audit_events WHERE operation='settings.update'"
                                 " ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(row, (CONTACT["address"], CONTACT["phone"], CONTACT["terms"]))
        recorded = json.loads(changes)
        self.assertEqual(recorded["terms"]["after"], CONTACT["terms"])
        self.assertEqual(recorded["address"]["after"], CONTACT["address"])


if __name__ == "__main__":
    unittest.main()
