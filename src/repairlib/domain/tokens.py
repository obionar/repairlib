"""Named token onboarding and bearer authentication."""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
import re
import secrets

from repairlib.contracts import API_TOKEN_FORMAT, TokenIdentity
from repairlib.domain.sessions import session_hash, verify_password
from repairlib.paths import database_path
from repairlib.storage import tokens
from repairlib.storage.tokens import TokenConflict, TokenMissing

REQUEST_KEY = re.compile(r"[0-9a-f]{32}")
AGENT_PREFIX = "agent: "
PAGE_SIZE = 25
MAX_RECORD_ID = (1 << 63) - 1
OnVerified = Callable[[tuple[int, int]], None]
# Static presets: the write resources (the `resource` an authorized call names) each one allows.
# Reads are never limited by a preset. A resource in no preset (users, CSV intake, portable
# archives, settings) is refused for every preset token.
PRESETS: dict[str, frozenset[str]] = {
    "read-only": frozenset(),
    "repairs-write": frozenset({"customers", "repairs"}),
    "inventory-write": frozenset({"inventory"}),
}


def token_scopes(role: str) -> tuple[str, ...]:
    return ("read", "write") if role in {"admin", "technician"} else ("read",)


def authenticate_api_token(workspace: Path, token: str) -> TokenIdentity | None:
    digest = api_token_hash(token)
    if digest:
        identity = tokens.identity(database_path(workspace), digest)
        if identity and identity.scope in token_scopes(identity.role):
            return identity
    return None


def api_token_hash(token: str) -> str | None:
    if API_TOKEN_FORMAT.fullmatch(token):
        return sha256(token.encode()).hexdigest()
    return None


def authenticated_digest(session: str) -> str:
    digest = session_hash(session)
    if digest is None:
        raise PermissionError("Sign in to manage API tokens.")
    return digest


def read_tokens(workspace: Path, session: str, *, now: int, before: int, on_verified: OnVerified | None = None):
    if not 0 <= before <= MAX_RECORD_ID:
        raise ValueError("Invalid token page.")
    digest = authenticated_digest(session)
    with tokens.token_read(database_path(workspace), digest, now) as (db, credential):
        if on_verified is not None:
            on_verified((credential.principal.shop_id, credential.principal.user_id))
        rows = tokens.list_owned(db, credential, before, PAGE_SIZE + 1)
    page = rows[:PAGE_SIZE]
    return credential.principal, page, page[-1].id if len(rows) > PAGE_SIZE else None


def read_token(workspace: Path, session: str, *, now: int, token_id: int, on_verified: OnVerified | None = None):
    if not 1 <= token_id <= MAX_RECORD_ID:
        raise TokenMissing("Token not found.")
    digest = authenticated_digest(session)
    with tokens.token_read(database_path(workspace), digest, now) as (db, owner):
        if on_verified is not None:
            on_verified((owner.principal.shop_id, owner.principal.user_id))
        return tokens.owned_token(db, owner, token_id)


def verified_session(workspace: Path, session: str, password: str, key: str, now: int,
                     on_verified: OnVerified | None = None):
    if not REQUEST_KEY.fullmatch(key):
        raise ValueError("Open the token form again to start a new request.")
    path = database_path(workspace)
    digest = session_hash(session)
    credential = tokens.credential_for_mutation(path, digest, now) if digest else None
    if credential is not None and on_verified is not None:
        on_verified((credential.principal.shop_id, credential.principal.user_id))
    if credential is None or not verify_password(credential.password_hash, password):
        raise PermissionError("Your current password or session could not be verified.")
    return path, digest, credential


def create_token(workspace: Path, session: str, *, password: str, name: str, scope: str,
                 key: str, now: int, kind: str = "user", preset: str | None = None,
                 on_verified: OnVerified | None = None) -> str | None:
    if kind not in {"user", "agent"}:
        raise ValueError("Choose whether the token is for a person or an agent.")
    if preset is not None and (kind != "agent" or preset not in PRESETS):
        raise ValueError("Choose a preset from the list, for an agent token only.")
    name = name.strip()
    if kind == "agent":
        name = AGENT_PREFIX + name
    elif name.lower().startswith("agent:"):
        raise ValueError('Only an agent token can be named "agent:".')
    if not 1 <= len(name) <= 80 or not name.isprintable():
        raise ValueError("Enter a token name of 1 to 80 printable characters.")
    if scope not in {"read", "write"}:
        raise ValueError("Choose a token scope.")
    path, digest, credential = verified_session(workspace, session, password, key, now, on_verified)
    if scope not in token_scopes(credential.principal.role):
        raise PermissionError("Your role cannot issue a token with this scope.")
    secret = "rl_" + secrets.token_urlsafe(32)
    with tokens.token_write(path, digest, credential, now) as db:
        issued = tokens.issue(db, credential, key=key, name=name, scope=scope, kind=kind,
                              preset=preset, token_hash=sha256(secret.encode()).hexdigest())
    return secret if issued else None


def revoke_token(workspace: Path, session: str, *, password: str, key: str, token_id: int,
                 revision: int, now: int, on_verified: OnVerified | None = None) -> None:
    if not 1 <= token_id <= MAX_RECORD_ID:
        raise TokenMissing("Token not found.")
    if not 1 <= revision < MAX_RECORD_ID:
        raise ValueError("Open the token again to review its revision.")
    path, digest, credential = verified_session(workspace, session, password, key, now, on_verified)
    with tokens.token_write(path, digest, credential, now) as db:
        tokens.revoke(db, credential.principal, key=key, token_id=token_id,
                      revision=revision, now=now)
