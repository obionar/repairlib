"""Shared, session-aware web navigation and its no-JavaScript controls."""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from repairlib.app import create_app
from test_dashboard import seed_dashboard
from test_tokens import ORIGIN, TokenFixture


def navigation(response):
    match = re.search(r'<nav [^>]*id="primary-navigation"[^>]*>.*?</nav>', response.text, re.S)
    if not match:
        raise AssertionError("Missing primary navigation")
    return match[0]


class WebShellTests(TokenFixture, TestCase):
    def test_compact_customer_register(self):
        with self.client() as client:
            self.sign_in(client)
            page = client.get('/customers')
            self.assertEqual(page.status_code, 200)
            self.assertIn('<h1 class="rl-page-title rl-sr-only">Customers</h1>', page.text)
            self.assertRegex(page.text, r'<section class="obi-panel" aria-labelledby="customer-search-title">\s*<header class="obi-panel-bar">\s*<h2 id="customer-search-title">Search</h2>')
            self.assertRegex(page.text, r'<input[^>]*id="customer-search"[^>]*placeholder="Search"')
            self.assertRegex(page.text, r'<button[^>]*type="submit"[^>]*aria-label="Search"')
            self.assertNotIn('type="checkbox"', page.text)
            self.assertRegex(page.text, r'<h2 id="customer-results">Customers</h2>\s*<a href="/customers/new">New customer</a>')
            form = client.get('/customers/new')
            self.assertRegex(form.text, r'<label class="rl-sr-only" for="name">Name</label>')
            self.assertRegex(form.text, r'<input[^>]*id="name"[^>]*placeholder="Name"')

    def test_repair_register_uses_search_and_results_title_bars(self):
        with self.client() as client:
            self.sign_in(client)
            page = client.get('/repairs?queue=open')
            self.assertEqual(page.status_code, 200)
            self.assertIn('<h1 class="rl-page-title rl-sr-only">Repairs</h1>', page.text)
            self.assertRegex(page.text, r'<section class="obi-panel" aria-labelledby="repair-search-title">\s*<header class="obi-panel-bar">\s*<h2 id="repair-search-title">Search</h2>')
            self.assertIn('<option value="open" selected>Open work</option>', page.text)
            self.assertRegex(page.text, r'<h2 id="repair-results">[^\n]+</h2>\s*<a href="/repairs/new">New repair</a>')
            self.assertEqual(page.text.count('href="/repairs/new"'), 1)

    def test_every_authenticated_page_has_primary_nav(self):
        seed_dashboard(self.db)
        with self.client() as client:
            self.sign_in(client)
            for path in ("/", "/repairs", "/repairs/6", "/repairs/new", "/customers",
                         "/customers/7", "/inventory", "/inventory/new", "/settings",
                         "/settings/printer", "/users", "/account", "/account/tokens"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text.count('<h1'), 1)
                    self.assertIn('<h1 class="rl-page-title rl-sr-only">', response.text)
                    nav = navigation(response)
                    for target in ("/", "/repairs", "/customers", "/inventory", "/settings", "/account"):
                        self.assertIn(f'href="{target}"', nav)
                    self.assertIn("Synthetic", nav)
                    self.assertIn("owner", nav)
                    self.assertEqual('id="language-choice"' in response.text, path == "/account")

    def test_inventory_and_users_creation_links_are_in_result_bars(self):
        with self.client() as client:
            self.sign_in(client)
            for path, heading, link, label in (
                ('/inventory', 'inventory-results', '/inventory/new', 'New inventory item'),
                ('/users', 'user-results', '/users/new', 'New user'),
            ):
                with self.subTest(path=path):
                    page = client.get(path)
                    self.assertEqual(page.status_code, 200)
                    self.assertRegex(page.text, fr'<h2 id="{heading}">[^<]+</h2>\s*<a href="{link}">{label}</a>')

    def test_repair_facts_hide_revision_but_stage_form_keeps_it(self):
        seed_dashboard(self.db)
        with self.client() as client:
            self.sign_in(client)
            page = client.get('/repairs/6')
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('<dt>Revision</dt>', page.text)
            stage = re.search(r'<form[^>]*id="stage-form"[^>]*>(.*?)</form>', page.text, re.S)
            self.assertIsNotNone(stage)
            self.assertIn('type="hidden" name="revision" value="1"', stage[1])
            self.assertIn('/static/app.css', page.text)
            self.assertNotIn('/static/account.css', page.text)

    def test_skip_link_targets_main(self):
        with self.client() as client:
            self.sign_in(client)
            html = client.get("/").text
            self.assertRegex(html, r'href="#main"[^>]*>Skip to content</a>')
            self.assertRegex(html, r'<main [^>]*id="main"[^>]*tabindex="-1"')

    def test_aria_current_marks_active_section(self):
        seed_dashboard(self.db)
        with self.client() as client:
            self.sign_in(client, "reader")
            for path, active in (("/", "/"), ("/repairs/6", "/repairs"),
                                 ("/customers/7", "/customers"), ("/inventory", "/inventory"),
                                 ("/account/tokens", "/account")):
                with self.subTest(path=path):
                    response = client.get(path)
                    nav = navigation(response)
                    self.assertRegex(nav, r'<a class="rl-brand" href="/"[^>]*>')
                    self.assertEqual(nav.count('href="/"'), 1)
                    self.assertRegex(response.text.split('<nav', 1)[0], r'<a class="rl-brand" href="/"[^>]*>')
                    self.assertEqual(re.findall(r'href="([^"]+)" aria-current="page"', nav), [active])
                    self.assertNotIn('href="/settings"', nav)
                    self.assertNotIn('href="/users"', nav)
                    self.assertNotIn("Outside shop", nav)
                    self.assertIn("Viewer", nav)
            self.assertEqual(client.get("/settings").status_code, 403)

    def test_mobile_nav_works_without_js(self):
        with self.client() as client:
            self.sign_in(client)
            response = client.get("/")
            self.assertRegex(response.text, r'<button [^>]*popovertarget="primary-navigation"[^>]*aria-label="Menu"')
            self.assertIn('<span class="rl-menu-text">Menu</span>', response.text)
            self.assertIn('<link rel="icon" type="image/svg+xml"', response.text)
            self.assertRegex(navigation(response), r'<nav [^>]*\bpopover(?=[\s>])')
            self.assertNotIn("<script", response.text)

    def test_anonymous_pages_have_no_business_nav(self):
        with self.client() as client:
            self.assertEqual(client.get("/").headers.get("location"), "/login")
            response = client.get("/login")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('id="primary-navigation"', response.text)
            self.assertIn('id="language-choice"', response.text)
        with TemporaryDirectory() as temp, TestClient(
            create_app(workspace=Path(temp) / "workspace"), base_url=ORIGIN
        ) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('id="primary-navigation"', response.text)
            self.assertIn('id="language-choice"', response.text)

    def test_sign_out_is_a_csrf_post_in_nav(self):
        with self.client() as client:
            self.sign_in(client)
            response = client.get("/repairs")
            self.assertRegex(navigation(response), r'<form method="post" action="/logout" id="nav-logout"')
            token = re.search(r'<input [^>]*form="nav-logout" name="csrf" value="([^"]+)"', response.text)
            self.assertIsNotNone(token)
            self.assertEqual(client.post("/logout").status_code, 403)
            self.assertEqual(client.get("/repairs").status_code, 200)
            signed_out = client.post("/logout", data={"csrf": token[1]})
            self.assertEqual((signed_out.status_code, signed_out.headers["location"]), (303, "/login"))
            self.assertEqual(client.get("/").headers["location"], "/login")
