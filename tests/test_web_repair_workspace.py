"""UI-4 repair workspace gates plus W3/W4/W5 regression checks."""

from contextlib import closing
from hashlib import sha256
import re
import sqlite3
import unittest
from uuid import uuid4

from repairlib.domain.repairs import STAGE_TRANSITIONS
from test_charges import PART
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret

ORIGINAL = b"\x00\xffWorkspace original bytes\r\n\x1a" * 32


def panel(page, name):
    html = page.text if hasattr(page, "text") else page
    match = re.search(rf'<section [^>]*aria-labelledby="{name}"[^>]*>(.*?)</section>', html, re.S)
    if not match:
        raise AssertionError(f"Missing panel {name}")
    return match[1]


def forms(html, action):
    return re.findall(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S)


def hidden(html, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    return match[1] if match else ""


def upload(client, auth, repair_id, content, filename="workspace.bin", **params):
    return client.post(f"{API}/{repair_id}/attachments", params={"filename": filename, **params},
                       headers={**auth, "Idempotency-Key": uuid4().hex,
                                "Content-Type": "application/octet-stream"}, content=content)


class WebRepairWorkspaceTests(TokenFixture, unittest.TestCase):
    def test_ticket_has_no_charge_editor_and_charges_show_in_payment(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            url = f"/repairs/{repair['id']}"
            page = client.get(url).text
            self.assertNotIn('aria-labelledby="parts"', page)
            self.assertNotIn("Charge type", page)
            self.assertNotIn('id="charge-details"', page)
            self.assertIn("Use part", panel(page, "stock-usage"))
            self.assertIn('class="rl-repair-work-grid"', page)
            saved = client.post(f"{API}/{repair['id']}/charges", json={
                "kind": "part", "description": "Bench part", "quantity_milli": 1000,
                "unit_price_cents": 3750}, headers={**auth, "Idempotency-Key": uuid4().hex})
            self.assertEqual(saved.status_code, 201, saved.text)
            self.assertIn("37.50", panel(client.get(url), "payment-total"))
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0], 0)

    def test_ticket_keeps_issue_and_intake_notes_with_status_controls(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth, serial="BENCH-0001", notes="Bring original cable")
            page = client.get(f"/repairs/{repair['id']}").text
            status = panel(page, "status")
            for text in ("Repair status", "Update status"):
                self.assertIn(text, status)
            self.assertNotIn("Next step", status)
            self.assertIn('<dt>Reported issue:</dt>', status)
            self.assertIn('<dt>Intake notes:</dt>', status)
            self.assertRegex(status, r'<div>\s*<dt>Serial:</dt>\s*<dd>BENCH-0001</dd>\s*</div>')
            self.assertIn("Bring original cable", status)
            self.assertIn('<option value="" selected disabled hidden>New</option>', status)
            self.assertNotIn('class="obi-status"', status)
            self.assertLess(status.index('<dt>Reported issue:</dt>'), status.index('id="ticket-controls"'))
            self.assertLess(status.index('id="ticket-controls"'), status.index('id="device-details"'))
            self.assertLess(status.index('id="stage-form"'), status.index('<dt>Intake notes:</dt>'))
            self.assertNotIn('id="repair-status-control"', status)
            controls = status.split('id="ticket-controls"', 1)[1]
            order = [controls.index(hook) for hook in ('id="stage-form"', 'id="pickup-state"',
                                                       'id="payment-state"', '<button type="submit" form="stage-form"')]
            self.assertEqual(order, sorted(order))
            self.assertIn('name="pickup" form="stage-form"', controls)
            self.assertNotIn("More actions", controls)
            self.assertNotIn("Technician", status)

    def test_ticket_facts_name_every_device_field(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth, serial="BENCH-0002")
            status = panel(client.get(f"/repairs/{repair['id']}").text, "status")
            for label, value in (("Device type", "Laptop"), ("Brand", "Synthetic"),
                                 ("Model", "Model Γ"), ("Serial", "BENCH-0002"),
                                 ("Display device", "Built-in display")):
                self.assertRegex(status, rf'<dt>{label}:</dt>\s*<dd[^>]*>\s*{re.escape(value)}\s*</dd>')
            self.assertIn('<div class="obi-panel-body" id="device-details">', status)
            empty = self.repair(client, auth, brand="", model="", serial="", display_device="", notes="")
            status = panel(client.get(f"/repairs/{empty['id']}").text, "status")
            for label in ("Brand", "Model", "Serial", "Display device", "Intake notes"):
                self.assertRegex(status, rf'<dt>{label}:</dt>\s*<dd[^>]*>\s*—\s*</dd>')

    def test_register_chip_tone_follows_stage_and_blank_facts_are_muted(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth, serial="")
            self.assertIn('class="obi-status" data-tone="new"', client.get("/repairs").text)
            status = panel(client.get(f"/repairs/{repair['id']}").text, "status")
            self.assertRegex(status, r'<dt>Serial:</dt><dd class="obi-muted">—</dd>')
            self.assertLess(status.index('<dt>Opened:</dt>'), status.index('id="device-details"'))

    def test_pages_opened_from_a_ticket_name_it_and_show_their_title(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            ticket = f"/repairs/{repair['id']}"
            for path in ("/entries/new", "/consume-part", "/edit"):
                page = client.get(ticket + path).text
                self.assertIn(f'<a class="rl-context-link" href="{ticket}">', page)
                self.assertIn('<h1 class="rl-page-title">', page)
                self.assertIn("rl-subpage", page)
            self.assertNotIn("rl-context-link", client.get(ticket).text)
            self.assertNotIn("rl-context-link", client.get("/repairs/new").text)

    def test_intake_picker_lists_contacts_and_leads_with_new_customer(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            created = client.post("/api/v1/customers", headers={**auth, "Idempotency-Key": uuid4().hex},
                                  json={"name": "Picker Person", "phone": "+30 210 000 0000"})
            self.assertEqual(created.status_code, 201, created.text)
            page = client.get("/repairs/new").text
            self.assertIn('<ul class="customer-list rl-pick-list">', page)
            self.assertIn("<span>+30 210 000 0000</span>", page)
            actions = page.split('rl-pick-actions">', 1)[1]
            self.assertLess(actions.index('class="obi-button">New customer</a>'),
                            actions.index("Continue without a customer"))

    def owner_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth, **fields):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={**DEVICE, **fields})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def entry(self, client, auth, repair_id, body="Synthetic note"):
        created = client.post(f"{API}/{repair_id}/entries",
                              headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={"kind": "work", "body": body})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def stage(self, client, auth, repair_id, stage, revision):
        changed = client.post(f"{API}/{repair_id}/stage",
                              headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={"revision": revision, "stage": stage, "reason": ""})
        self.assertEqual(changed.status_code, 200, changed.text)
        return changed.json()

    def test_stage_choices_keep_order_and_disable_unavailable_transitions(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            order = ("new", "in_progress", "waiting_parts", "lab_test", "customer_test",
                     "repaired", "no_repair")
            self.assertEqual(set(order), set(STAGE_TRANSITIONS))
            for current in order:
                with self.subTest(current=current):
                    with closing(sqlite3.connect(self.db)) as db, db:
                        db.execute("UPDATE repairs SET stage=? WHERE id=?", (current, repair["id"]))
                    status = panel(client.get(f"/repairs/{repair['id']}"), "status")
                    select = re.search(r'<select id="stage" name="stage"[^>]*>(.*?)</select>', status, re.S)
                    if select is None:
                        self.fail("Missing status dropdown")
                    choices = re.findall(r'<option value="([a-z_]+)"( disabled)?>', select[1])
                    self.assertEqual(tuple(name for name, _ in choices), order)
                    self.assertEqual({name for name, disabled in choices if not disabled},
                                     STAGE_TRANSITIONS[current])
                    self.assertIn(f'<option value="" selected disabled hidden>{current.replace("_", " ").title()}</option>', select[1])

    def test_status_panel_has_no_reason_field_even_from_terminal_stage(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            open_page = client.get(f"/repairs/{repair['id']}")
            self.assertNotIn('name="reason"', panel(open_page, "status"))
            self.stage(client, auth, repair["id"], "in_progress", 1)
            repaired = self.stage(client, auth, repair["id"], "repaired", 2)
            status = panel(client.get(f"/repairs/{repair['id']}"), "status")
            self.assertNotIn('name="reason"', status)
            self.assertNotIn("Reason for reopening", status)
            self.assertEqual(repaired["stage"], "repaired")

    def test_inline_entry_add_returns_to_log_anchor(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            previous = self.entry(client, auth, repair["id"], "Earlier bench note")
            url = f"/repairs/{repair['id']}"
            log = panel(client.get(url), "log")
            composer, = forms(log, f"{url}/entries")
            self.assertLess(log.index('id="entry-body"'), log.index(f'id="entry-{previous["id"]}"'))
            self.assertIn('name="body"', composer)
            self.assertIn('name="kind"', composer)
            reply = client.post(f"{url}/entries", data={
                "csrf": hidden(composer, "csrf"), "key": uuid4().hex,
                "kind": "contact", "body": "Called the synthetic customer",
            })
            self.assertEqual(reply.status_code, 303)
            self.assertEqual(reply.headers["location"], url + "?saved=entry#log")
            self.assertIn("Called the synthetic customer", panel(client.get(url), "log"))
            refused = client.post(f"{url}/entries", data={
                "csrf": hidden(composer, "csrf"), "key": uuid4().hex,
                "kind": "contact", "body": " ",
            })
            self.assertEqual(refused.status_code, 422)
            self.assertIn("Enter work-entry text", refused.text)

    def test_attachment_link_uses_entry_choice(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            first = self.entry(client, auth, repair["id"], "First measured value")
            second = self.entry(client, auth, repair["id"], "Second measured value")
            created = upload(client, auth, repair["id"], ORIGINAL)
            self.assertEqual(created.status_code, 201, created.text)
            file = created.json()
            files = panel(client.get(f"/repairs/{repair['id']}"), "files")
            self.assertNotIn('name="entry_id" inputmode="numeric"', files)
            select = re.search(r'<select id="link-entry-\d+" name="entry_id"[^>]*>(.*?)</select>',
                               files, re.S)
            self.assertIsNotNone(select, "Link control must be an entry select")
            self.assertIn(f'value="{first["id"]}"', select[1])
            self.assertIn(f'value="{second["id"]}"', select[1])
            link_form, = forms(files, f"/repairs/{repair['id']}/attachments/{file['id']}/links")
            self.assertIn('name="linked" value="1"', link_form)
            linked = client.post(
                f"/repairs/{repair['id']}/attachments/{file['id']}/links",
                data={"csrf": hidden(link_form, "csrf"), "key": uuid4().hex,
                      "revision": hidden(link_form, "revision"), "linked": "1",
                      "entry_id": str(second["id"])},
            )
            self.assertEqual(linked.status_code, 303)
            self.assertIn(f"Entry {second['id']}",
                          panel(client.get(f"/repairs/{repair['id']}"), "files"))

    def test_mobile_order_matches_dom_order(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            page = client.get(f"/repairs/{repair['id']}").text
            order = [page.index(f'aria-labelledby="{name}"') for name in
                     ("status", "log", "payment-total", "files", "activity-heading")]
            self.assertEqual(order, sorted(order))
            self.assertNotIn('aria-labelledby="activity"', page)

    def test_viewer_sees_no_forms_or_costs(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            self.entry(client, auth, repair["id"])
            client.post(f"{API}/{repair['id']}/charges", json=PART,
                        headers={**auth, "Idempotency-Key": uuid4().hex})
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute(
                    """INSERT INTO users(id, shop_id, username, role, password_hash)
                       SELECT 2, 1, 'reader', 'viewer', password_hash FROM users WHERE username = 'owner'"""
                )
        with self.client() as viewer:
            self.sign_in(viewer, "reader")
            page = viewer.get(f"/repairs/{repair['id']}")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(forms(page.text, f"/repairs/{repair['id']}/entries"), [])
            self.assertNotIn('name="unit_cost"', page.text)
            self.assertNotIn('name="stage"', page.text)
            self.assertIn('aria-labelledby="payment-total"', page.text)
            self.assertEqual(forms(page.text, f"/repairs/{repair['id']}/fees"), [])
            self.assertNotIn('name="labor"', page.text)

    def test_log_pages_keep_older_entry_reachable_and_selectable(self):
        # W3: an older live entry must stay reachable, correctable and selectable.
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            first = self.entry(client, auth, repair["id"], "Oldest measured value")
            for number in range(25):
                self.entry(client, auth, repair["id"], f"Bench note {number}")
            url = f"/repairs/{repair['id']}"
            log = panel(client.get(url), "log")
            self.assertNotIn("Oldest measured value", log)
            older = re.search(r'href="([^"]*log_before=[^"]+)"[^>]*>Older entries', log)
            self.assertIsNotNone(older, "Log needs an older-entries link")
            second_log = panel(client.get(older[1].replace("&amp;", "&")), "log")
            self.assertIn("Oldest measured value", second_log)
            self.assertIn(f'value="{first["id"]}"', second_log)
            self.assertIn(f'/repairs/{repair["id"]}/entries/{first["id"]}/edit', second_log)

    def test_active_file_pages_survive_detached_history(self):
        # W4: filter statuses before limiting so an old active file stays downloadable.
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            created = upload(client, auth, repair["id"], ORIGINAL, filename="active.bin")
            self.assertEqual(created.status_code, 201, created.text)
            active = created.json()
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("PRAGMA foreign_keys = ON")
                db.executemany(
                    """INSERT INTO attachments
                       (id, shop_id, repair_id, uploader_id, uploader_username, filename,
                        media_type, size_bytes, sha256, storage_name, detached_at)
                       VALUES (?, 1, ?, 1, 'owner', ?, 'application/octet-stream', 10, ?, ?, '2026-01-02')""",
                    [(i, repair["id"], f"detached-{i}.bin", "0" * 64, f"{i:032x}")
                     for i in range(2, 102)],
                )
            page = client.get(f"/repairs/{repair['id']}")
            files = panel(page, "files")
            self.assertIn("active.bin", files)
            content = client.get(
                f"/repairs/{repair['id']}/attachments/{active['id']}/content")
            self.assertEqual(content.status_code, 200)
            self.assertEqual(sha256(content.content).hexdigest(), sha256(ORIGINAL).hexdigest())
            history = re.search(r'<summary>Detached files[^<]*</summary>', files)
            self.assertIsNotNone(history)
            self.assertIn("Older detached files", files)


class WorkspaceFeedbackTests(TokenFixture, unittest.TestCase):
    def owner_auth(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def repair(self, client, auth, **fields):
        created = client.post(API, headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={**DEVICE, **fields})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def entry(self, client, auth, repair_id, body="Synthetic note"):
        created = client.post(f"{API}/{repair_id}/entries",
                              headers={**auth, "Idempotency-Key": uuid4().hex},
                              json={"kind": "work", "body": body})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def stocked_repair(self, client, auth):
        repair = self.repair(client, auth)
        item = client.post("/api/v1/inventory", headers={**auth, "Idempotency-Key": uuid4().hex},
                           json={"sku": "USB-C-01", "description": "Charging port", "category": "Ports",
                                 "manufacturer": "", "part_number": "", "package": "",
                                 "specifications": "", "bin": "", "unit": "pcs", "low_stock_milli": 0,
                                 "purchase_cost_cents": 1200, "selling_price_cents": 1999}).json()
        received = client.post(f"/api/v1/inventory/{item['id']}/receive",
                               headers={**auth, "Idempotency-Key": uuid4().hex},
                               json={"revision": item["revision"], "quantity_milli": 5000,
                                     "reason": "Delivery"}).json()
        item = received["item"]
        charge = client.post(f"{API}/{repair['id']}/consume-part",
                             headers={**auth, "Idempotency-Key": uuid4().hex},
                             json={"item_id": item["id"], "repair_revision": repair["revision"],
                                   "item_revision": item["revision"], "consumed_quantity_milli": 1000,
                                   "billed_quantity_milli": 1000, "unit_price_cents": 1999})
        self.assertEqual(charge.status_code, 201, charge.text)
        return repair, item, charge.json()

    def test_saved_notices_render_inside_the_target_panel(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair, _, _ = self.stocked_repair(client, auth)
            self.entry(client, auth, repair["id"])
            upload(client, auth, repair["id"], ORIGINAL)
            url = f"/repairs/{repair['id']}"
            for code, name, message in (
                ("stage", "status", "Repair status updated."),
                ("entry", "log", "Work entry added."),
                ("charge", "payment-total", "Charge saved."),
                ("attachment", "files", "File saved."),
                ("pickup", "status", "Pickup state saved."),
            ):
                page = client.get(url + f"?saved={code}")
                self.assertIn(message, panel(page, name))
                self.assertGreater(page.text.index(f'aria-labelledby="{name}"'),
                                   page.text.index('class="rl-page-header'))

    def test_validation_error_has_a_focused_summary_at_the_top(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            url = f"/repairs/{repair['id']}"
            composer, = forms(panel(client.get(url), "log"), f"{url}/entries")
            reply = client.post(f"{url}/entries", data={
                "csrf": hidden(composer, "csrf"), "key": uuid4().hex,
                "kind": "work", "body": " ",
            })
            self.assertEqual(reply.status_code, 422)
            html = reply.text
            self.assertLess(html.index('id="error-summary"'), html.index('id="status"'))
            summary = re.search(r'<div[^>]*id="error-summary"[^>]*>', html).group(0)
            self.assertIn('role="alert"', summary)
            self.assertIn('tabindex="-1"', summary)
            self.assertIn("autofocus", summary)
            self.assertIn('href="#entry-body"', html)
            self.assertIn("Enter work-entry text.", html)
            self.assertIn('aria-invalid="true"', html)
            self.assertEqual(html.count('role="alert"'), 1)

    def test_viewer_and_archived_record_hide_the_stock_return_link(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair, _, charge = self.stocked_repair(client, auth)
            url = f"/repairs/{repair['id']}"
            self.assertIn("/return-part", client.get(url).text)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("""INSERT INTO users(id, shop_id, username, role, password_hash)
                              SELECT 2, 1, 'reader', 'viewer', password_hash
                              FROM users WHERE username = 'owner'""")
        with self.client() as viewer:
            self.sign_in(viewer, "reader")
            self.assertNotIn("/return-part", viewer.get(url).text)
        with self.client() as client:
            self.sign_in(client)
            archived = client.post(f"{API}/{repair['id']}/archive",
                                   headers={**auth, "Idempotency-Key": uuid4().hex},
                                   json={"revision": repair["revision"], "enabled": True})
            self.assertEqual(archived.status_code, 200, archived.text)
            page = client.get(url).text
            self.assertIn("read-only for stock", page)
            self.assertNotIn("/return-part", page)

    def test_focused_subpages_link_back_to_their_panel(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair, item, charge = self.stocked_repair(client, auth)
            entry = self.entry(client, auth, repair["id"])
            record = f"/repairs/{repair['id']}"
            record_page = client.get(record).text
            cases = [
                (f"{record}/charges/new", "payment-total"),
                (f"{record}/charges/{charge['id']}/return-part", "payment-total"),
                (f"{record}/consume-part/{item['id']}", "log"),
                (f"{record}/entries/new", "log"),
                (f"{record}/entries/{entry['id']}/history", "log"),
                (f"{record}/entries/{entry['id']}/visibility", "log"),
            ]
            for path, panel_name in cases:
                page = client.get(path)
                self.assertEqual(page.status_code, 200, path)
                self.assertNotIn("#repair-charges", page.text)
                link = re.search(rf'<a href="{re.escape(record)}#([a-z-]+)">Back to repair</a>',
                                 page.text)
                self.assertIsNotNone(link, path)
                self.assertEqual(link.group(1), panel_name, path)
                self.assertIn(f'aria-labelledby="{panel_name}"', record_page)

    def test_paged_counts_link_placeholder_and_localized_file_dates(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            for number in range(26):
                self.entry(client, auth, repair["id"], f"Bench note {number}")
            upload(client, auth, repair["id"], ORIGINAL, filename="active.bin")
            detached = upload(client, auth, repair["id"], ORIGINAL, filename="detached.bin").json()
            client.post(f"{API}/{repair['id']}/attachments/{detached['id']}/detach",
                        headers={**auth, "Idempotency-Key": uuid4().hex},
                        json={"revision": detached["revision"], "detached": True})
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("PRAGMA foreign_keys = ON")
                db.executemany(
                    """INSERT INTO attachments
                       (id, shop_id, repair_id, uploader_id, uploader_username, filename,
                        media_type, size_bytes, sha256, storage_name, detached_at)
                       VALUES (?, 1, ?, 1, 'owner', ?, 'application/octet-stream', 10, ?, ?,
                               '2026-01-02T03:04:05.000Z')""",
                    [(i, repair["id"], f"history-{i}.bin", "0" * 64, f"{i:032x}")
                     for i in range(10, 36)],
                )
            page = client.get(f"/repairs/{repair['id']}")
            self.assertIn("25 shown", panel(page, "log"))
            link_select = re.search(r'<select id="link-entry-\d+" name="entry_id"[^>]*>(.*?)</select>',
                                    panel(page, "files"), re.S)
            self.assertIsNotNone(link_select)
            self.assertIn("Choose an entry", link_select[1])
            self.assertRegex(link_select[1], r'<option value=""[^>]*>')
            detached_panel = re.search(r'<summary>Detached files.*?</details>',
                                       page.text, re.S).group(0)
            self.assertNotRegex(detached_panel, r"\d{4}-\d{2}-\d{2}T")
            self.assertIn("Detached 2026-01-02 03:04 · original bytes kept", detached_panel)
            self.assertIn("Older detached files", panel(page, "files"))

    def test_greek_repair_workspace_has_no_english_controls(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            repair = self.repair(client, auth)
            self.entry(client, auth, repair["id"])
            upload(client, auth, repair["id"], ORIGINAL)
            client.get("/language", params={"lang": "el"})
            page = client.get(f"/repairs/{repair['id']}")
            self.assertEqual(page.status_code, 200)
            for label in ("Edit details", "Add entry", "Delete entry", "Restore entry",
                          "Save correction", "Detach file", "Next step", "Work log"):
                self.assertNotIn(label, page.text)
            self.assertIn("Επεξεργασία στοιχείων", page.text)

    def test_register_status_chips_keep_the_search_and_mark_the_active_status(self):
        with self.client() as client:
            _, auth = self.owner_auth(client)
            self.repair(client, auth)
            plain = client.get("/repairs")
            links = re.findall(r'class="rl-chip" href="([^"]+)"', plain.text)
            self.assertGreaterEqual(len(links), 2)
            self.assertNotIn("q=", links[0])
            self.assertRegex(plain.text, r'<a class="rl-chip" href="/repairs"[^>]*aria-current="page"')
            searched = client.get("/repairs?q=Bench")
            self.assertRegex(searched.text, r'href="/repairs\?stage=[^"&]+&amp;q=Bench"')
            picked = re.search(r'<a class="rl-chip" href="(/repairs\?stage=[^"]+)"', plain.text)[1]
            active = client.get(picked.replace("&amp;", "&"))
            self.assertIn(f'<a class="rl-chip" href="{picked}" aria-current="page">', active.text)
            empty = client.get("/repairs?q=zzz-no-such-repair")
            self.assertIn("No repairs match these filters.", empty.text)
