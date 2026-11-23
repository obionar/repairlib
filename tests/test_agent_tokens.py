"""AGENT-1 gate: agent tokens are marked at issue and attributed on every write."""

from contextlib import closing
import json
import sqlite3
from unittest import TestCase, main
from uuid import uuid4

import test_tokens
from test_tokens import ACCOUNT, IDENTITY, PASSWORD, hidden, issued_secret

CUSTOMERS = "/api/v1/customers"
FIELDS = {"name": "Synthetic Agent Customer", "phone": "", "email": "", "address": "", "notes": ""}


class AgentTokenTests(test_tokens.TokenFixture, TestCase):
    def issue(self, client, **overrides):
        reply = client.post(ACCOUNT, data=self.issue_form(client, **overrides))
        self.assertEqual(reply.status_code, 201, reply.text)
        return issued_secret(reply)

    def write(self, client, token, **extra):
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex, **extra}
        return client.post(CUSTOMERS, headers=headers, json=FIELDS)

    def audit(self, operation="customer.create"):
        return self.snapshot(
            "SELECT actor_kind, actor_user_id, actor_username, source, token_label "
            "FROM audit_events WHERE operation = ? ORDER BY id", (operation,))

    def test_issue_marks_kind_and_label_and_never_stores_the_secret(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            self.issue(client, name="Bench script", kind="user", key=uuid4().hex)
            self.assertEqual(self.snapshot("SELECT name, kind FROM api_tokens ORDER BY id"),
                             [("agent: Intake bot", "agent"), ("Bench script", "user")])
            self.assertEqual(self.snapshot("SELECT actor_kind, source, changes FROM audit_events "
                                           "WHERE operation = 'token.create' ORDER BY id")[0][:2],
                             ("user", "web"))
            created = self.snapshot("SELECT changes FROM audit_events WHERE operation = 'token.create' "
                                    "ORDER BY id")
            self.assertEqual(json.loads(created[0][0])["kind"], "agent")
            self.assertEqual(json.loads(created[1][0])["kind"], "user")
            page = client.get(ACCOUNT).text
            self.assertIn("agent: Intake bot", page)
            self.assertNotIn(agent, page)
            for _, changes in self.snapshot("SELECT operation, changes FROM audit_events"):
                self.assertNotIn(agent, changes or "")
            # A kind must be chosen from the known values; an omitted kind stays a person's token.
            bad = client.post(ACCOUNT, data=self.issue_form(client, name="X", kind="robot",
                                                            key=uuid4().hex))
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(client.get(IDENTITY, headers={"Authorization": f"Bearer {agent}"}
                                        ).json()["kind"], "agent")

    def test_every_agent_write_records_agent_identity_and_kind(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            human = self.issue(client, name="Bench script", kind="user", key=uuid4().hex)
            customer_id = self.write(client, agent).json()["id"]
            self.assertEqual(self.write(client, human).status_code, 201)
            form = client.get("/customers/new")
            web = client.post("/customers/new", data={**FIELDS, "csrf": hidden(form, "csrf"),
                                                      "key": hidden(form, "key")})
            self.assertEqual(web.status_code, 303)
            history = client.get(f"{CUSTOMERS}/{customer_id}/history",
                                 headers={"Authorization": f"Bearer {agent}"}).json()
            self.assertEqual([event["actor_kind"] for event in history["items"]], ["agent"])
        self.assertEqual(self.audit(), [
            ("agent", 1, "owner", "api", "agent: Intake bot"),
            ("user", 1, "owner", "api", "Bench script"),
            ("user", 1, "owner", "web", None),
        ])

    def test_agent_writes_through_other_families_are_marked(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            headers = {"Authorization": f"Bearer {agent}", "Idempotency-Key": uuid4().hex}
            made = client.post("/api/v1/repairs", headers=headers, json={
                "device_type": "Phone", "brand": "Synthetic", "model": "One",
                "reported_issue": "Synthetic fault"})
            self.assertEqual(made.status_code, 201, made.text)
            repair_id = made.json()["id"]
            activity = client.get(f"/api/v1/repairs/{repair_id}/activity",
                                  headers={"Authorization": f"Bearer {agent}"}).json()
            self.assertEqual({event["actor_kind"] for event in activity["items"]}, {"agent"})
        self.assertEqual(self.audit("repair.create")[0][0], "agent")

    def test_revoked_agent_token_stops_working(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            self.assertEqual(self.write(client, agent).status_code, 201)
            (token_id,), = self.snapshot("SELECT id FROM api_tokens")
            revoke = f"{ACCOUNT}/{token_id}/revoke"
            page = client.get(revoke)
            reply = client.post(revoke, data={"csrf": hidden(page, "csrf"), "key": hidden(page, "key"),
                                              "revision": hidden(page, "revision"), "password": PASSWORD})
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(self.write(client, agent).status_code, 401)
            self.assertEqual(client.get(IDENTITY, headers={"Authorization": f"Bearer {agent}"}
                                        ).status_code, 401)
        self.assertEqual(len(self.audit()), 1)

    def test_a_token_cannot_impersonate_another_actor_or_claim_to_be_human(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO users(id, shop_id, username, role, password_hash) "
                       "SELECT 2, 1, 'technician', 'technician', password_hash FROM users WHERE id = 1")
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            spoof = {"X-Actor": "technician", "X-Actor-Kind": "user", "X-Actor-User-Id": "2",
                     "X-Forwarded-User": "technician", "X-Token-Label": "Front desk"}
            self.assertEqual(self.write(client, agent, **spoof).status_code, 201)
            for field in ("actor_kind", "actor_username", "actor_user_id", "kind", "token_label"):
                reply = client.post(CUSTOMERS, headers={"Authorization": f"Bearer {agent}",
                                                        "Idempotency-Key": uuid4().hex},
                                    json={**FIELDS, field: "user"})
                self.assertEqual(reply.status_code, 422, field)
            self.assertEqual(self.audit(), [("agent", 1, "owner", "api", "agent: Intake bot")])
            # A person's token may not borrow the agent marker, and a kind cannot be rewritten.
            borrowed = client.post(ACCOUNT, data=self.issue_form(client, name="Agent: Intake bot",
                                                                 kind="user", key=uuid4().hex))
            self.assertEqual(borrowed.status_code, 422)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM api_tokens"), [(1,)])
        with closing(sqlite3.connect(self.db)) as db:
            for sql in ("UPDATE api_tokens SET kind = 'user'",
                        "UPDATE api_tokens SET name = 'Front desk'",
                        "INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, scope, "
                        "create_key, kind) VALUES (1, 1, 1, 'h', 'Front desk', 'write', 'k', 'agent')",
                        "INSERT INTO audit_events(shop_id, actor_kind, actor_user_id, actor_username, "
                        "operation, source, token_label) VALUES (1, 'agent', 1, 'owner', 'x', 'web', NULL)",
                        "INSERT INTO audit_events(shop_id, actor_kind, actor_user_id, actor_username, "
                        "operation, source, token_label) VALUES (1, 'agent', 1, 'owner', 'x', 'api', "
                        "'agent: forged')"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    db.execute(sql)

    def test_a_human_session_cannot_act_as_an_agent_nor_use_a_token_as_a_session(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            session = client.cookies.get("session") or next(iter(client.cookies.jar)).value
            # The session cookie is not a bearer token, so it never reaches the agent path.
            self.assertEqual(client.post(CUSTOMERS, headers={"Authorization": f"Bearer {session}",
                                                             "Idempotency-Key": uuid4().hex},
                                         json=FIELDS).status_code, 401)
            form = client.get("/customers/new")
            reply = client.post("/customers/new", data={
                **FIELDS, "csrf": hidden(form, "csrf"), "key": hidden(form, "key"),
                "actor_kind": "agent", "token_label": "agent: Intake bot", "source": "api"})
            self.assertEqual(reply.status_code, 400)  # extra fields are refused outright
            form = client.get("/customers/new")
            reply = client.post("/customers/new", data={
                **FIELDS, "csrf": hidden(form, "csrf"), "key": hidden(form, "key")})
            self.assertEqual(reply.status_code, 303)
        with self.client() as client:
            # An agent token is no browser credential and cannot mint or revoke tokens.
            client.cookies.set("session", agent)
            self.assertEqual(client.get(ACCOUNT).status_code, 303)
            self.assertEqual(client.post(ACCOUNT, data={"name": "Child", "scope": "write",
                                                        "key": uuid4().hex, "password": PASSWORD}
                                         ).status_code, 403)
        self.assertEqual(self.audit(), [("user", 1, "owner", "web", None)])

    def test_audit_and_export_distinguish_agent_without_the_secret(self):
        with self.client() as client:
            self.sign_in(client)
            agent = self.issue(client, name="Intake bot", kind="agent")
            self.assertEqual(self.write(client, agent).status_code, 201)
            self.assertEqual(self.write(client, agent).status_code, 201)
            customer_id = 1
            export = client.get(f"{CUSTOMERS}/{customer_id}/export",
                                headers={"Authorization": f"Bearer {agent}"})
            self.assertNotIn(agent, export.text)
            history = client.get(f"{CUSTOMERS}/{customer_id}/history",
                                 headers={"Authorization": f"Bearer {agent}"})
            self.assertNotIn(agent, history.text)
            self.assertEqual(history.json()["items"][0]["token_label"], "agent: Intake bot")
            self.assertEqual(history.json()["items"][0]["actor_kind"], "agent")
        from repairlib.storage import portable_shop
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            exported = [dict(row) for row in portable_shop.rows(db, 1, "audit_events")]
        marked = [row for row in exported if row["operation"] == "customer.create"]
        self.assertTrue(marked)
        self.assertEqual({(row["actor_kind"], row["source"]) for row in marked}, {("user", "agent")})
        self.assertNotIn(agent, json.dumps(exported))


PRESETS = ("read-only", "repairs-write", "inventory-write")
ITEM = {"sku": "SYN-PRESET-1", "description": "Synthetic preset part", "selling_price_cents": 1250}
REPAIR = {"device_type": "Phone", "brand": "Synthetic", "model": "One",
          "reported_issue": "Synthetic fault", "notes": "Synthetic repair text"}
CONTACT = {"name": "Synthetic Masked Customer", "phone": "+357 555 0199",
           "email": "masked@example.test", "address": "9 Synthetic Street", "notes": "Synthetic note"}
MASK = "[masked]"
ALLOWED = {
    "read-only": set(),
    "repairs-write": {"customer", "repair"},
    "inventory-write": {"inventory"},
}


class PresetTests(test_tokens.TokenFixture, TestCase):
    def issue(self, client, preset=None, **overrides):
        extra = {"preset": preset} if preset is not None else {}
        reply = client.post(ACCOUNT, data=self.issue_form(
            client, kind="agent", key=uuid4().hex, **extra, **overrides))
        self.assertEqual(reply.status_code, 201, reply.text)
        return issued_secret(reply)

    def attempts(self, client, token):
        """One write per family; returns {family: response}."""
        def headers():
            return {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex}
        return {
            "customer": client.post(CUSTOMERS, headers=headers(), json=FIELDS),
            "repair": client.post("/api/v1/repairs", headers=headers(), json=REPAIR),
            "inventory": client.post("/api/v1/inventory", headers=headers(), json=ITEM),
        }

    def rows(self):
        counts = {}
        for table in ("customers", "repairs", "inventory_items", "customer_requests",
                      "repair_requests", "inventory_requests"):
            counts[table] = self.snapshot(f"SELECT COUNT(*) FROM {table}")[0][0]
        return counts

    def test_each_preset_allows_its_operations_and_refuses_the_others_with_a_code(self):
        for preset in PRESETS:
            with self.subTest(preset=preset), self.client() as client:
                self.sign_in(client)
                token = self.issue(client, preset=preset)
                before = self.rows()
                replies = self.attempts(client, token)
                for family, reply in replies.items():
                    if family in ALLOWED[preset]:
                        self.assertEqual(reply.status_code, 201, (preset, family, reply.text))
                    else:
                        self.assertEqual(reply.status_code, 403, (preset, family))
                        self.assertEqual(reply.json()["error"]["code"], "preset_forbidden")
                        self.assertNotEqual(reply.json()["error"]["code"], "forbidden")
                        self.assertIn(preset, reply.json()["error"]["message"])
                after = self.rows()
                created = {"customer": ("customers", "customer_requests"),
                           "repair": ("repairs", "repair_requests"),
                           "inventory": ("inventory_items", "inventory_requests")}
                for family, tables in created.items():
                    grew = 1 if family in ALLOWED[preset] else 0
                    for table in tables:
                        self.assertEqual(after[table] - before[table], grew, (preset, family, table))

    def test_every_preset_still_reads(self):
        for preset in PRESETS:
            with self.subTest(preset=preset), self.client() as client:
                self.sign_in(client)
                token = self.issue(client, preset=preset)
                auth = {"Authorization": f"Bearer {token}"}
                for path in (CUSTOMERS, "/api/v1/repairs", "/api/v1/inventory", IDENTITY):
                    self.assertEqual(client.get(path, headers=auth).status_code, 200, (preset, path))

    def test_preset_refusal_is_not_reachable_through_other_write_families(self):
        with self.client() as client:
            self.sign_in(client)
            token = self.issue(client, preset="read-only")
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex}
            users_before = self.snapshot("SELECT COUNT(*) FROM users")
            reply = client.post("/api/v1/users", headers=headers, json={
                "username": "intruder", "role": "viewer", "password": PASSWORD})
            self.assertIn(reply.status_code, (403, 422))
            if reply.status_code == 403:
                self.assertEqual(reply.json()["error"]["code"], "preset_forbidden")
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM users"), users_before)

    def test_a_token_without_a_preset_keeps_todays_behaviour(self):
        with self.client() as client:
            self.sign_in(client)
            token = self.issue(client)
            self.assertEqual(self.snapshot("SELECT preset FROM api_tokens"), [(None,)])
            self.assertEqual({family: reply.status_code
                              for family, reply in self.attempts(client, token).items()},
                             {"customer": 201, "repair": 201, "inventory": 201})

    def test_a_preset_is_an_agent_choice_from_the_static_list_and_cannot_change(self):
        with self.client() as client:
            self.sign_in(client)
            for bad in ("admin", "repairs-write,inventory-write"):  # the password budget allows 5 a minute
                reply = client.post(ACCOUNT, data=self.issue_form(
                    client, kind="agent", preset=bad, key=uuid4().hex))
                self.assertEqual(reply.status_code, 422, bad)
            person = client.post(ACCOUNT, data=self.issue_form(
                client, kind="user", preset="read-only", key=uuid4().hex))
            self.assertEqual(person.status_code, 422)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM api_tokens"), [(0,)])
            self.issue(client, preset="read-only")
            created = self.snapshot("SELECT changes FROM audit_events WHERE operation = 'token.create'")
            self.assertEqual(json.loads(created[0][0])["preset"], "read-only")
        tables = {name for (name,) in self.snapshot("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse({name for name in tables if "preset" in name})  # no user-editable table
        with closing(sqlite3.connect(self.db)) as db:
            for sql in ("UPDATE api_tokens SET preset = NULL",
                        "UPDATE api_tokens SET preset = 'repairs-write'",
                        "INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, "
                        "scope, create_key, kind, preset) VALUES (1, 1, 1, 'h2', 'agent: X', 'write', "
                        "'k2', 'agent', 'superuser')",
                        "INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, "
                        "scope, create_key, kind, preset) VALUES (1, 1, 1, 'h3', 'Person', 'write', "
                        "'k3', 'user', 'read-only')"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    db.execute(sql)

    def test_the_refusal_is_audited_as_the_agent_and_leaves_nothing_else(self):
        with self.client() as client:
            self.sign_in(client)
            token = self.issue(client, preset="inventory-write", name="Stock bot")
            before = self.rows()
            reply = client.post(CUSTOMERS, headers={"Authorization": f"Bearer {token}",
                                                    "Idempotency-Key": uuid4().hex}, json=FIELDS)
            self.assertEqual(reply.status_code, 403)
            self.assertEqual(self.rows(), before)
            self.assertNotIn(token, reply.text)
        events = self.snapshot("SELECT actor_kind, actor_username, source, token_label, changes "
                               "FROM audit_events WHERE operation = 'token.preset_refused'")
        self.assertEqual(len(events), 1)
        kind, username, source, label, changes = events[0]
        self.assertEqual((kind, username, source, label), ("agent", "owner", "api", "agent: Stock bot"))
        self.assertEqual(json.loads(changes)["preset"], "inventory-write")
        self.assertNotIn(token, changes)
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE operation = 'customer.create'"),
                         [(0,)])

    def test_the_refusal_message_has_a_greek_catalog_entry(self):
        from repairlib import i18n
        key = "The {preset} preset of this token does not allow changing {resource}."
        self.assertIn(key, i18n.CATALOGS["en"]["messages"])
        self.assertNotEqual(i18n.CATALOGS["el"]["messages"].get(key, key), key)

    def test_a_dry_run_refusal_writes_no_audit_row(self):
        from repairlib.contracts import CustomerInput
        from repairlib.domain import dry_run
        from repairlib.domain.customers import CustomerAccess, CustomerError, create_customer
        with self.client() as client:
            self.sign_in(client)
            token = self.issue(client, preset="read-only")
        before = self.snapshot("SELECT COUNT(*) FROM audit_events")
        with dry_run.armed():
            with self.assertRaises(CustomerError) as caught:
                create_customer(self.workspace, CustomerAccess("api", token),
                                CustomerInput(**FIELDS), key=uuid4().hex)
        self.assertEqual(caught.exception.code, "preset_forbidden")
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events"), before)


class MaskingTests(test_tokens.TokenFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.clients = []

    def tokens(self, client):
        self.sign_in(client)
        out = {}
        for kind in ("agent", "user"):
            reply = client.post(ACCOUNT, data=self.issue_form(client, name=f"{kind} masking",
                                                              kind=kind, key=uuid4().hex))
            self.assertEqual(reply.status_code, 201)
            out[kind] = {"Authorization": f"Bearer {issued_secret(reply)}"}
        return out

    def seed(self, client, auth):
        made = client.post(CUSTOMERS, headers={**auth, "Idempotency-Key": uuid4().hex}, json=CONTACT)
        self.assertEqual(made.status_code, 201, made.text)
        customer_id = made.json()["id"]
        repair = client.post("/api/v1/repairs", headers={**auth, "Idempotency-Key": uuid4().hex},
                             json={**REPAIR, "customer_id": customer_id})
        self.assertEqual(repair.status_code, 201, repair.text)
        patched = client.patch(f"{CUSTOMERS}/{customer_id}",
                               headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"revision": 1, "phone": "+357 555 0200"})
        self.assertEqual(patched.status_code, 200, patched.text)
        return customer_id, repair.json()

    def surfaces(self, customer_id):
        return {"detail": f"{CUSTOMERS}/{customer_id}", "list": CUSTOMERS,
                "export": f"{CUSTOMERS}/{customer_id}/export",
                "history": f"{CUSTOMERS}/{customer_id}/history"}

    def test_agent_sees_masked_contact_data_and_a_person_sees_it_whole(self):
        with self.client() as client:
            auth = self.tokens(client)
            customer_id, repair = self.seed(client, auth["user"])
            secrets_ = ("555 0199", "555 0200", "masked@example.test", "9 Synthetic Street")
            for name, path in self.surfaces(customer_id).items():
                person = client.get(path, headers=auth["user"])
                agent = client.get(path, headers=auth["agent"])
                self.assertEqual((person.status_code, agent.status_code), (200, 200), name)
                for value in secrets_:
                    self.assertNotIn(value, agent.text, (name, value))
                if name != "history":
                    self.assertIn("555 0200", person.text, name)
                    self.assertIn("masked@example.test", person.text, name)
                    self.assertIn(MASK, agent.text, name)
            detail = client.get(f"{CUSTOMERS}/{customer_id}", headers=auth["agent"]).json()
            self.assertEqual((detail["phone"], detail["email"], detail["address"]), (MASK, MASK, MASK))
            self.assertEqual((detail["id"], detail["name"], detail["notes"], detail["revision"]),
                             (customer_id, f"C{customer_id}", CONTACT["notes"], 2))  # name hidden as C<id>
            human = client.get(f"{CUSTOMERS}/{customer_id}", headers=auth["user"]).json()
            self.assertEqual((human["phone"], human["email"], human["address"]),
                             ("+357 555 0200", "masked@example.test", "9 Synthetic Street"))
            # An empty field stays empty, so the agent can tell "none on file" from "hidden".
            blank = client.post(CUSTOMERS, headers={**auth["user"], "Idempotency-Key": uuid4().hex},
                                json=FIELDS).json()["id"]
            self.assertEqual(client.get(f"{CUSTOMERS}/{blank}", headers=auth["agent"]).json()["phone"], "")
            # Masking is serialization only: the stored row is untouched.
        self.assertEqual(self.snapshot("SELECT phone, email, address FROM customers WHERE id = ?",
                                       (customer_id,)),
                         [("+357 555 0200", "masked@example.test", "9 Synthetic Street")])

    def test_masking_leaves_repairs_money_ids_and_text_intact(self):
        with self.client() as client:
            auth = self.tokens(client)
            customer_id, repair = self.seed(client, auth["user"])
            repair_id = repair["id"]
            charge = client.post(f"/api/v1/repairs/{repair_id}/charges",
                                 headers={**auth["user"], "Idempotency-Key": uuid4().hex},
                                 json={"kind": "labor", "description": "Synthetic labour", "quantity_milli": 1000,
                                       "unit_price_cents": 4500})
            self.assertEqual(charge.status_code, 201, charge.text)
            for path in (f"/api/v1/repairs/{repair_id}", "/api/v1/repairs",
                         f"/api/v1/repairs/{repair_id}/charges",
                         f"/api/v1/repairs/{repair_id}/activity"):
                agent = client.get(path, headers=auth["agent"])
                person = client.get(path, headers=auth["user"])
                self.assertEqual(agent.status_code, 200, path)
                expected = person.json()
                if path == "/api/v1/repairs":  # the only change: the customer name becomes C<id>
                    for row in expected["items"]:
                        row["customer_name"] = f"C{row['customer_id']}" if row["customer_id"] else None
                self.assertEqual(agent.json(), expected, path)
            self.assertIn("Synthetic repair text", client.get(
                f"/api/v1/repairs/{repair_id}", headers=auth["agent"]).text)
            self.assertIn("4500", client.get(
                f"/api/v1/repairs/{repair_id}/charges", headers=auth["agent"]).text)
            export = client.get(f"{CUSTOMERS}/{customer_id}/export", headers=auth["agent"]).json()
            reference = client.get(f"{CUSTOMERS}/{customer_id}/export", headers=auth["user"]).json()
            self.assertEqual(export["repairs"], reference["repairs"])
            for field in ("id", "notes", "revision", "created_at"):
                self.assertEqual(export["customer"][field], reference["customer"][field], field)
            self.assertEqual(export["customer"]["name"], f"C{customer_id}")  # the name is hidden as C<id>
            history = client.get(f"{CUSTOMERS}/{customer_id}/history", headers=auth["agent"]).json()
            change = next(e for e in history["items"] if e["operation"] == "customer.update")
            self.assertEqual(change["changes"]["phone"], {"before": MASK, "after": MASK})
            self.assertEqual([e["id"] for e in history["items"]],
                             [e["id"] for e in client.get(f"{CUSTOMERS}/{customer_id}/history",
                                                          headers=auth["user"]).json()["items"]])

    def test_a_masked_write_response_and_replay_hide_contact_data_from_the_agent_only(self):
        with self.client() as client:
            auth = self.tokens(client)
            key = uuid4().hex
            first = client.post(CUSTOMERS, headers={**auth["agent"], "Idempotency-Key": key}, json=CONTACT)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(first.json()["phone"], MASK)
            replay = client.post(CUSTOMERS, headers={**auth["agent"], "Idempotency-Key": key}, json=CONTACT)
            self.assertEqual(replay.json()["phone"], MASK)
            self.assertEqual(self.snapshot("SELECT phone FROM customers"), [("+357 555 0199",)])


if __name__ == "__main__":
    main()
