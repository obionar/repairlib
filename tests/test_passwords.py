"""Password hashing stays identical across argon2-cffi releases (Debian 21.1 to current)."""

import unittest

from repairlib.domain.passwords import INVALID_PASSWORD, hash_with_salt, hasher
from repairlib.domain.sessions import verify_password

PARAMETERS = "$argon2id$v=19$m=65536,t=3,p=4$"


class PasswordTests(unittest.TestCase):
    def test_new_hashes_use_explicit_production_parameters(self):
        self.assertTrue(hasher().hash("synthetic passphrase").startswith(PARAMETERS))
        self.assertTrue(hash_with_salt("synthetic passphrase", b"0123456789abcdef").startswith(PARAMETERS))

    def test_salted_hash_is_stable_and_verifies(self):
        first = hash_with_salt("synthetic passphrase", b"0123456789abcdef")
        self.assertEqual(first, hash_with_salt("synthetic passphrase", b"0123456789abcdef"))
        self.assertNotEqual(first, hash_with_salt("synthetic passphrase", b"fedcba9876543210"))
        self.assertTrue(verify_password(first, "synthetic passphrase"))
        self.assertFalse(verify_password(first, "another passphrase"))

    def test_malformed_hash_is_refused_not_raised(self):
        self.assertFalse(verify_password("not a hash", "synthetic passphrase"))
        self.assertEqual(len(INVALID_PASSWORD), 2)


if __name__ == "__main__":
    unittest.main()
