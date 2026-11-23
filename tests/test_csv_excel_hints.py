"""Files saved by Excel on a Greek PC (semicolons, Windows-1253) get a refusal that says how to fix them."""

import unittest

from repairlib.domain.csv_intake import read_records

LIMITS = {"name": 160, "phone": 80, "email": 254, "address": 500, "notes": 1000}
SAVE_AS = 'save it as "CSV UTF-8 (Comma delimited)"'


class ExcelHintTests(unittest.TestCase):
    def test_semicolon_file_is_refused_with_the_save_as_hint(self):
        records, errors = read_records("name;phone;email;address;notes\r\nΓιώργος;69;;;\r\n".encode(), LIMITS)
        self.assertEqual(records, [])
        self.assertEqual([(e.row, e.field) for e in errors], [(1, "csv")])
        self.assertIn("semicolons", errors[0].message)
        self.assertIn(SAVE_AS, errors[0].message)

    def test_windows_greek_encoding_is_refused_with_the_save_as_hint(self):
        raw = "name,phone,email,address,notes\r\nΓιώργος,69,,,\r\n".encode("cp1253")
        records, errors = read_records(raw, LIMITS)
        self.assertEqual((records, errors[0].row), ([], 2))
        self.assertIn(SAVE_AS, errors[0].message)

    def test_a_wrong_header_keeps_the_template_message(self):
        _, errors = read_records(b"nom,phone,email,address,notes\r\n", LIMITS)
        self.assertEqual(errors[0].message, "Use the exact template header.")


if __name__ == "__main__":
    unittest.main()
