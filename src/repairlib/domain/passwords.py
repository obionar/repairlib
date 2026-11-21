"""One Argon2id configuration for every password hash.

The parameters are explicit (RFC 9106 low-memory profile) instead of the library
defaults, which differ between argon2-cffi releases such as the 21.1 shipped by
Debian stable and the current one. Existing hashes carry their own parameters.
"""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.low_level import hash_secret
from argon2.exceptions import VerificationError

try:  # argon2-cffi 23.1 renamed InvalidHash
    from argon2.exceptions import InvalidHashError
except ImportError:  # pragma: no cover - older distro packages
    from argon2.exceptions import InvalidHash as InvalidHashError

INVALID_PASSWORD = (InvalidHashError, VerificationError)


def hasher() -> PasswordHasher:
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_with_salt(password: str, salt: bytes) -> str:
    """Argon2id hash with a caller-chosen salt; PasswordHasher.hash(salt=) needs argon2-cffi 23.1."""
    return hash_secret(password.encode(), salt, time_cost=3, memory_cost=65536, parallelism=4,
                       hash_len=32, type=Type.ID).decode()
