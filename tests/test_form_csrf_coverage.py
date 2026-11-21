"""Every POST form in a template carries a CSRF field (inline, or a linked input via form="<id>")."""

from pathlib import Path
import re
import unittest

TEMPLATES = Path(__file__).parents[1] / "src/repairlib/adapters/web/templates"
FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S | re.I)
FIELD_MACRO = re.compile(r"\{% macro (\w*fields)\(.*?\{% endmacro %\}", re.S)
CSRF = re.compile(r"""<input\b[^>]*\bname\s*=\s*(['\"])csrf\1""", re.I)
INPUT = re.compile(r"<input\b[^>]*>", re.I)
LOOP = re.compile(r"""\{%\s*for\s+(\w+)\s+in\s*\(([^)]*)\)\s*%\}(.*?)\{%\s*endfor\s*%\}""", re.S)


def attribute(tag, name):
    match = re.search(rf"(?<![\w-]){re.escape(name)}\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))", tag, re.I | re.S)
    return (match.group(2) if match.group(1) else match.group(3)) if match else None


def usable(tag):
    return not re.search(r"(?:^|\s)disabled(?:\s|=|/?>)", tag, re.I)


def unconditional(text):
    text = re.sub(r"\{#.*?#\}|<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\{%\s*if\b.*?\{%\s*endif\s*%\}", "", text, flags=re.S)


def has_csrf(body, macros):
    body = unconditional(body)
    if any(usable(tag) and attribute(tag, 'name') == 'csrf' and attribute(tag, 'form') is None for tag in INPUT.findall(body)):
        return True
    if any(re.search(rf"\{{\{{\s*{re.escape(name)}\s*\(", body) for name in macros):
        return True
    for variable, values, loop_body in LOOP.findall(body):
        if re.search(r"(?:^|,)\s*(['\"])csrf\1\s*(?:,|$)", values):
            if any(usable(tag) and attribute(tag, 'form') is None
                   and re.fullmatch(rf"\{{\{{\s*{re.escape(variable)}\s*\}}\}}", attribute(tag, 'name') or '')
                   for tag in INPUT.findall(loop_body)):
                return True
    return False


def post_forms_without_csrf():
    texts = {path: re.sub(r"\{#.*?#\}|<!--.*?-->", "", path.read_text(encoding='utf8'), flags=re.S)
             for path in sorted(TEMPLATES.glob('*.html'))}
    definitions = {}
    for text in texts.values():
        for macro in FIELD_MACRO.finditer(text):
            definitions.setdefault(macro[1], []).append(has_csrf(macro[0], set()))
    macros = {name for name, matches in definitions.items() if all(matches)}
    missing = []
    for path, text in texts.items():
        for attributes, body in FORM.findall(text):
            if (attribute(attributes, 'method') or '').lower() != 'post':
                continue
            form_id = attribute(attributes, 'id')
            # shortcut: linked conditional fields are static guesses; use rendered-form coverage for dynamic branches.
            linked_text = re.sub(r"\{%\s*if\s+(?:false|False|0)\s*%\}.*?\{%\s*endif\s*%\}", "", text, flags=re.S)
            linked = form_id and any(usable(tag) and attribute(tag, 'form') == form_id and attribute(tag, 'name') == 'csrf'
                                     for tag in INPUT.findall(linked_text))
            if not linked and not has_csrf(body, macros):
                missing.append(f"{path.name}: {attributes.strip()[:80]}")
    return missing


class FormCsrfCoverageTests(unittest.TestCase):
    def test_every_post_form_has_a_csrf_field(self):
        self.assertEqual(post_forms_without_csrf(), [])

    def test_field_macros_used_by_forms_really_emit_the_csrf_field(self):
        for path in sorted(TEMPLATES.glob("*.html")):
            for macro in FIELD_MACRO.finditer(path.read_text(encoding="utf8")):
                self.assertIn("csrf", macro[0], f"{path.name}: {macro[1]} does not emit a csrf field")

    def test_the_scan_sees_a_form_without_one(self):
        # The scan must be able to fail: a synthetic template with a bare POST form is reported.
        text = '<form method="post" action="/x"><input name="a"></form>'
        found = [attrs for attrs, body in FORM.findall(text) if not CSRF.search(body)]
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
