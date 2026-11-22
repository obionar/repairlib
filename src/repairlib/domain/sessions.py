"""Password verification and opaque, expiring browser sessions."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import re
import secrets
from pathlib import Path

from repairlib.domain.passwords import INVALID_PASSWORD, hasher
from repairlib.paths import database_path
from repairlib.storage import sessions, workspace_records

SESSION_SECONDS = 12 * 60 * 60
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
# A synthetic hash with the same Argon2 settings as first-admin credentials.
DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$BeDG8PL+6vU8U9eFlh7edg"
    "$98iI+kxc68tpSkhKBSEd0vVhqrlA6DpxER9oUEV3Jp4"
)


def verify_password(password_hash: str, password: str) -> bool:
    if not 1 <= len(password) <= 1024:
        return False
    try:
        return hasher().verify(password_hash, password)
    except INVALID_PASSWORD:
        return False


# Public credentials of a synthetic demo; the login page shows them only while they still work.
DEMO_LOGIN = ("demo", "demo")


@lru_cache(maxsize=8)
def is_demo_password(password_hash: str) -> bool:
    return verify_password(password_hash, DEMO_LOGIN[1])


def demo_login(workspace: Path) -> tuple[str, str] | None:
    """The demo credentials, if this is a seeded synthetic demo and they still sign in."""
    path = database_path(workspace)
    if not path.exists() or not workspace_records.synthetic_demo(path):
        return None
    credential = sessions.lookup_credential(path, DEMO_LOGIN[0])
    return DEMO_LOGIN if credential and is_demo_password(credential.password_hash) else None


def login(workspace: Path, username: str, password: str, *, now: int) -> str | None:
    username = username.strip()
    if not 1 <= len(username) <= 80 or not 1 <= len(password) <= 1024:
        return None
    path = database_path(workspace)
    credential = sessions.lookup_credential(path, username)
    if not verify_password(credential.password_hash if credential else DUMMY_HASH, password) or credential is None:
        return None
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    if sessions.issue_session(path, credential, token_hash, now, now + SESSION_SECONDS):
        return token
    return None


def session_hash(token: str) -> str | None:
    if TOKEN_FORMAT.fullmatch(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()
    return None


def principal_for_session(workspace: Path, token: str, *, now: int) -> sessions.Principal | None:
    token_hash = session_hash(token)
    if token_hash:
        return sessions.session_principal(database_path(workspace), token_hash, now)
    return None


def logout(workspace: Path, token: str, *, now: int) -> None:
    token_hash = session_hash(token)
    if token_hash:
        sessions.revoke_session(database_path(workspace), token_hash, now)
