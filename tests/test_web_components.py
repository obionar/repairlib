"""UI-7a component invariants over the server-rendered pages."""

from contextlib import closing
from html.parser import HTMLParser
import sqlite3
import unittest
from uuid import uuid4

from test_charges import PART
from test_repairs import DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret

REPAIRS = "/api/v1/repairs"
PRIMARY = {"obi-primary", "obi-button--primary"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.primaries = []
        self.headings = []
        self.tables = []
        self.labels = set()
        self.controls = []
        self.images = []
        self.styles = []
        self.scripts = []
        self._table = None
        self._row = None
        self._in_thead = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if PRIMARY & set(classes):
            self.primaries.append((tag, attributes))
        if tag in HEADINGS:
            self.headings.append(int(tag[1]))
        if tag == "label" and attributes.get("for"):
            self.labels.add(attributes["for"])
        if tag in ("input", "select", "textarea"):
            self.controls.append((tag, attributes))
        if tag == "img":
            self.images.append(attributes)
        if "style" in attributes:
            self.styles.append((tag, attributes))
        if tag == "script":
            self.scripts.append(attributes)
        if tag == "table":
            self._table = []
            self.tables.append(self._table)
        if tag == "thead" and self._table is not None:
            self._in_thead = True
        if tag == "tr" and self._table is not None:
            self._row = {"header": self._in_thead, "cells": [], "classes": []}
            self._table.append(self._row)
        if self._row is not None:
            self._row["classes"].extend(classes)
            if tag == "th":
                self._row["cells"].append(attributes)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_thead = False
        elif tag == "tr":
            self._row = None
        elif tag == "table":
            self._table = None
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def parse(html):
    document = Document()
    document.feed(html)
    return document


def primary_problems(html):
    return [tag for tag, _ in parse(html).primaries][1:]


def heading_problems(html):
    levels = parse(html).headings
    problems = [] if levels.count(1) == 1 else [levels]
    for previous, level in zip([0, *levels], levels):
        if level > previous + 1:
            problems.append(level)
    return problems


def table_problems(html):
    problems = []
    for number, rows in enumerate(parse(html).tables, 1):
        header = next((row for row in rows if row["header"]), rows[0] if rows else None)
        if header is None or not any(cell.get("scope") in ("col", "row") for cell in header["cells"]):
            problems.append(number)
    return problems


def control_problems(html):
    document = parse(html)
    problems = []
    for tag, attributes in document.controls:
        if tag == "input" and attributes.get("type") == "hidden":
            continue
        if attributes.get("id") in document.labels or attributes.get("aria-label"):
            continue
        problems.append(attributes.get("name") or tag)
    return problems


def image_problems(html):
    return [image for image in parse(html).images if "alt" not in image]


def script_style_problems(html):
    document = parse(html)
    problems = [tag for tag, _ in document.styles]
    problems += [attributes for attributes in document.scripts
                 if not attributes.get("src", "").startswith("/static/")]
    return problems


def interactive_rows(html):
    return [row for table in parse(html).tables for row in table
            if any(cell.get("scope") == "row" for cell in row["cells"])]


def rows_with_actions(html):
    return [row for row in interactive_rows(html) if "obi-actions" in row["classes"]]


GOOD = """<h1>Title</h1><h2>Section</h2>
<a class="obi-primary">Go</a>
<table><thead><tr><th scope="col">Name</th></tr></thead>
<tbody><tr><th scope="row">Row</th><td><div class="obi-actions"><a>Edit</a></div></td></tr></tbody></table>
<label for="q">Query</label><input id="q"><img src="/static/x.png" alt="">
<script src="/static/app.js"></script>"""


class ComponentRuleSelfTests(unittest.TestCase):
    """Each rule must reject a deliberately broken fixture."""

    def test_primary_action_rule(self):
        self.assertEqual(primary_problems(GOOD), [])
        self.assertTrue(primary_problems(GOOD + '<a class="obi-primary">Again</a>'))
        self.assertTrue(primary_problems(GOOD + '<button class="obi-button--primary">Again</button>'))

    def test_heading_rule(self):
        self.assertEqual(heading_problems(GOOD), [])
        self.assertTrue(heading_problems("<h1>One</h1><h1>Two</h1><h2>Section</h2>"))
        self.assertTrue(heading_problems("<h1>One</h1><h3>Skipped</h3>"))
        self.assertTrue(heading_problems("<h2>No title</h2>"))

    def test_table_header_rule(self):
        self.assertEqual(table_problems(GOOD), [])
        self.assertTrue(table_problems("<table><thead><tr><th>Bare</th></tr></thead></table>"))

    def test_control_label_rule(self):
        self.assertEqual(control_problems(GOOD), [])
        broken = '<input id="q"><select name="s"><option>x</option></select><textarea name="t"></textarea>'
        self.assertEqual(len(control_problems(broken)), 3)
        self.assertEqual(len(control_problems('<input type="hidden" name="csrf">')), 0)
        self.assertEqual(image_problems('<img src="/static/x.png">'), [{"src": "/static/x.png"}])

    def test_row_action_rule(self):
        self.assertEqual(len(rows_with_actions(GOOD)), 1)
        bare = GOOD.replace('<div class="obi-actions"><a>Edit</a></div>', "<a>Edit</a>")
        self.assertEqual(rows_with_actions(bare), [])

    def test_no_inline_style_or_script_rule(self):
        self.assertEqual(script_style_problems(GOOD), [])
        self.assertTrue(script_style_problems('<p style="color:red">x</p>'))
        self.assertTrue(script_style_problems("<script>alert(1)</script>"))
        self.assertTrue(script_style_problems('<script src="/elsewhere/app.js"></script>'))


class ComponentPageTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}"}

            def headers():
                return {**auth, "Idempotency-Key": uuid4().hex}

            customer = client.post("/api/v1/customers", headers=headers(),
                                   json={"name": "Synthetic component customer", "phone": "+357 555"})
            self.assertEqual(customer.status_code, 201)
            self.customer_id = customer.json()["id"]
            repair = client.post(REPAIRS, headers=headers(),
                                 json={**DEVICE, "customer_id": self.customer_id})
            self.assertEqual(repair.status_code, 201)
            self.repair_id = repair.json()["id"]
            charge = client.post(f"{REPAIRS}/{self.repair_id}/charges", headers=headers(), json=PART)
            self.assertEqual(charge.status_code, 201)
            item = client.post("/api/v1/inventory", headers=headers(), json={
                "sku": "SCREEN-1", "description": "Display module", "unit": "pcs",
                "selling_price_cents": 2500,
            })
            self.assertEqual(item.status_code, 201)

    def viewer(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO users(shop_id, username, role, password_hash) "
                "SELECT 1, 'viewer', 'viewer', password_hash FROM users WHERE username = 'owner'"
            )
        client = self.client()
        self.addCleanup(client.__exit__, None, None, None)
        client.__enter__()
        self.sign_in(client, "viewer")
        return client

    def assert_invariants(self, html, name):
        self.assertEqual(primary_problems(html), [], f"{name}: more than one primary action")
        self.assertEqual(heading_problems(html), [], f"{name}: heading level skipped")
        self.assertEqual(table_problems(html), [], f"{name}: table header without scope")
        self.assertEqual(control_problems(html), [], f"{name}: control without a label")
        self.assertEqual(image_problems(html), [], f"{name}: image without alt")
        self.assertEqual(script_style_problems(html), [], f"{name}: inline style or script")

    def test_pages_hold_component_invariants(self):
        paths = {
            "dashboard": "/",
            "customer list": "/customers",
            "customer detail": f"/customers/{self.customer_id}",
            "repair list": "/repairs",
            "repair detail": f"/repairs/{self.repair_id}",
            "inventory list": "/inventory",
            "settings": "/settings",
            "account": "/account",
        }
        with self.client() as client:
            self.sign_in(client)
            for name, path in paths.items():
                with self.subTest(page=name):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assert_invariants(response.text, name)
            viewer = self.viewer()
            errors = (("403", "/settings", viewer),
                      ("404", f"/repairs/{self.repair_id + 1000}", client))
            for name, path, session in errors:
                with self.subTest(page=name):
                    response = session.get(path)
                    self.assertEqual(response.status_code, int(name))
                    self.assert_invariants(response.text, name)

    def test_customer_list_opens_records_without_inline_edit_actions(self):
        with self.client() as client:
            self.sign_in(client)
            customers = interactive_rows(client.get("/customers").text)
            self.assertTrue(customers)
            self.assertEqual(rows_with_actions(client.get("/customers").text), [])
            self.assertNotIn(">Actions</th>", client.get("/customers").text)
        viewer = self.viewer()
        for path in ("/customers", "/repairs", "/inventory"):
            with self.subTest(path=path):
                self.assertEqual(rows_with_actions(viewer.get(path).text), [])
