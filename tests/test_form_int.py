"""One strict parser for numeric form fields (revisions, versions, small limits)."""

import unittest

from repairlib.adapters.web.security import form_int


class FormIntTests(unittest.TestCase):
    def test_accepts_only_plain_ascii_digits_within_the_length_bound(self):
        self.assertEqual([form_int(raw) for raw in ("7", "0", "0042", "9" * 19)], [7, 0, 42, int("9" * 19)])
        for refused in ("", " ", "1 ", "-1", "+1", "1.5", "1e3", "１２", "²", "9" * 20, "abc"):
            self.assertEqual(form_int(refused), 0, refused)

    def test_a_smaller_digit_bound_is_honoured(self):
        self.assertEqual((form_int("99999", 5), form_int("100000", 5)), (99999, 0))


if __name__ == "__main__":
    unittest.main()
