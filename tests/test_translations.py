"""S3 translations: external catalogs, English/Greek coverage, language choice and fallback."""

import json
from pathlib import Path
import re
import string
import unittest

from repairlib import i18n
from repairlib.domain.repairs import ENTRY_KINDS, STAGE_TRANSITIONS
from repairlib.domain.users import ROLES
from test_repairs import API, DEVICE
from test_tokens import TokenFixture
from test_users import ACCOUNT, issued_secret

ROOT = Path(__file__).resolve().parents[1] / "src" / "repairlib"
TEMPLATES = ROOT / "adapters" / "web" / "templates"
CALL = re.compile(r"""\b_\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""")
UI_CALL = re.compile(r"ui\.\w+\((?:[^()]|\([^()]*\))*\)", re.S)
UI_LABEL = re.compile(r"'([A-Z][^']*)'")
VISIBLE_TEXT = re.compile(r">\s*([A-Za-z][^<>{}]*[A-Za-z.])\s*<")


def template_keys() -> set[str]:
    keys = set()
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        for single, double in CALL.findall(text):
            keys.add(single or double)
        for call in UI_CALL.findall(text):
            keys.update(UI_LABEL.findall(call))
    return keys


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def test_catalogs_are_external_complete_and_placeholder_safe(self):
        self.assertEqual(set(i18n.LANGUAGES), {"en", "el"})
        english = json.loads((ROOT / "locales" / "en.json").read_text())["messages"]
        greek = json.loads((ROOT / "locales" / "el.json").read_text())["messages"]
        used = template_keys()
        self.assertGreater(len(used), 150)
        self.assertEqual(used - set(english), set(), "template text missing from en.json")
        labels = {value.replace("_", " ").title() for value in (
            *STAGE_TRANSITIONS, *ENTRY_KINDS, *ROLES, "part")}  # Estimate statuses retired; labour is labelled "Labour".
        self.assertEqual(labels - set(english), set(), "enum labels missing from en.json")
        self.assertEqual(set(greek) - set(english), set(), "el.json has keys English lacks")
        self.assertEqual(set(english) - set(greek), set(), "el.json must cover every English key")
        for key, text in english.items():
            self.assertEqual(text, key)
            self.assertEqual(fields(greek[key]), fields(key), key)
            self.assertNotEqual(greek[key].strip(), "", key)

    def test_templates_have_no_untranslated_visible_literals(self):
        for path in TEMPLATES.glob("*.html"):
            for text in set(VISIBLE_TEXT.findall(path.read_text())) - {"RepairLib"}:  # product name
                self.fail(f"{path.name}: untranslated text {text!r}")

    def test_negotiation_and_fallback(self):
        self.assertEqual(i18n.negotiate("el", ""), "el")
        self.assertEqual(i18n.negotiate("", "fr-FR, el-GR;q=0.8, en;q=0.5"), "el")
        self.assertEqual(i18n.negotiate("xx", "de"), "en")
        self.assertEqual(i18n.negotiate("", "el;q=0"), "en")
        greek = i18n.translator("el")
        self.assertEqual(greek("Repairs"), "Επισκευές")
        self.assertEqual(greek("Not in any catalog {n}", n=3), "Not in any catalog 3")
        self.assertEqual(greek("Repair {id}", id=5), "Επισκευή 5")


class LanguageChoiceTests(TokenFixture, unittest.TestCase):
    def test_language_choice_survives_pages_and_rejects_open_redirects(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {"Authorization": f"Bearer {token}", "Idempotency-Key": "a" * 32}
            repair = client.post(API, headers=auth, json=DEVICE).json()
            english = client.get("/repairs")
            self.assertIn('<html lang="en">', english.text)
            self.assertIn("New repair", english.text)
            chosen = client.get("/language", params={"lang": "el", "next": "/repairs"}, follow_redirects=False)
            self.assertEqual(chosen.status_code, 303)
            self.assertEqual(chosen.headers["location"], "/repairs")
            greek = client.get("/repairs")
            self.assertIn('<html lang="el">', greek.text)
            self.assertIn("Νέα επισκευή", greek.text)
            self.assertNotIn("New repair", greek.text)
            detail = client.get(f"/repairs/{repair['id']}")
            self.assertIn("Εργασία EUR", detail.text)
            self.assertIn(">Χρήση ανταλλακτικού</button>", detail.text)
            picker = client.get(f"/repairs/{repair['id']}/consume-part")
            self.assertNotIn('id="manual-part-form"', picker.text)
            self.assertIn("Ανταλλακτικά καταλόγου", picker.text)
            self.assertIn("Στοιχεία συσκευής", detail.text)
            self.assertIn("Does not power on", detail.text)  # user data is never translated
            self.assertIn("Επισκευή", client.get(f"/repairs/{repair['id']}/print").text)
            self.assertIn('<option value="el" selected>', client.get("/account").text)
            for target in ("https://example.invalid/", "//example.invalid", "/\\example.invalid"):
                away = client.get("/language", params={"lang": "en", "next": target}, follow_redirects=False)
                self.assertEqual(away.headers["location"], "/")
            unknown = client.get("/language", params={"lang": "xx", "next": "/repairs"}, follow_redirects=False)
            self.assertEqual(unknown.status_code, 303)
            self.assertIn('<html lang="en">', client.get("/repairs").text)
            self.assertEqual(client.get(API, headers={"Authorization": f"Bearer {token}"}).status_code, 200)

    def test_accept_language_without_cookie(self):
        with self.client() as client:
            page = client.get("/login", headers={"Accept-Language": "el-GR,el;q=0.9"})
            self.assertIn('<html lang="el">', page.text)
            self.assertIn("Σύνδεση", page.text)


if __name__ == "__main__":
    unittest.main()
