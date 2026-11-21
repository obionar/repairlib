"""The CSRF form guard must inspect real candidate forms, not incidental text."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import test_form_csrf_coverage as guard


class CsrfGuardRegression(TestCase):
    def scan(self, template):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'probe.html').write_text(template, encoding='utf8')
            with patch.object(guard, 'TEMPLATES', directory):
                return guard.post_forms_without_csrf()

    def test_single_quoted_post_without_field_is_reported(self):
        self.assertTrue(self.scan("<form method='post'><input name='x'></form>"))

    def test_unquoted_post_without_field_is_reported(self):
        self.assertTrue(self.scan('<form method=post><input name=x></form>'))

    def test_unquoted_csrf_input_is_accepted(self):
        self.assertFalse(self.scan('<form method=post><input name=csrf></form>'))

    def test_unrelated_macro_is_not_a_csrf_field(self):
        self.assertTrue(self.scan('<form method="post">{{ unrelated_fields() }}</form>'))

    def test_incidental_csrf_string_is_not_a_field(self):
        self.assertTrue(self.scan('<form method="post">{{ helper(\'csrf\') }}</form>'))

    def test_unsuccessful_or_unrendered_inputs_are_rejected(self):
        for template in (
            '<form method=post><input name=csrf disabled></form>',
            '<form method=post>{# <input name=csrf> #}</form>',
            '<form method=post>{% if false %}<input name=csrf>{% endif %}</form>',
            '<form method=post>{% for field in ("csrf",) %}<input name="{{ field }}" form=other>{% endfor %}</form>',
            '{% macro hidden_fields() %}<input name=csrf form=other>{% endmacro %}<form method=post>{{ hidden_fields() }}</form>',
        ):
            with self.subTest(template=template):
                self.assertTrue(self.scan(template))

    def test_single_quoted_input_and_linked_input_are_accepted(self):
        self.assertFalse(self.scan("<form method='post'><input name='csrf'></form>"))
        self.assertFalse(self.scan("<form id='f' method='post'></form><input name='csrf' form='f'>"))
