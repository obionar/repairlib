"""Shop-scoped API tokens and atomic, replayable token changes."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from repairlib.contracts import TokenIdentity
from repairlib.storage.sessions import Credential, Principal, connect, session_credential


class TokenConflict(Exception):
    """The request key or reviewed token revision has changed."""


class TokenMissing(Exception):
    """The token is unavailable to this account."""


@dataclass(frozen=True)
class NamedToken:
    id: int
    name: str
    scope: str
    revision: int
    kind: str = "user"
    preset: str | None = None


def identity(path: Path, token_hash: str) -> TokenIdentity | None:
    with closing(connect(path, "ro")) as db:
        return identity_in(db, token_hash)


def identity_in(db: sqlite3.Connection, token_hash: str) -> TokenIdentity | None:
    # Migration 31 makes the "agent: " name prefix and `kind` agree on every row, so the prefix
    # answers for databases that are read before they are upgraded. Migration 32 added `preset`, and a
    # database read before that upgrade simply has no preset tokens: NULL is the truthful answer, and
    # it keeps recovery of a prior-schema workspace working.
    has_preset = any(row[1] == "preset" for row in db.execute("PRAGMA table_info(api_tokens)"))
    preset = "t.preset" if has_preset else "NULL"
    row = db.execute(
        f"""
        SELECT u.shop_id, u.id, u.username, u.role, t.scope, t.name,
               CASE WHEN lower(t.name) LIKE 'agent: %' THEN 'agent' ELSE 'user' END, {preset}
        FROM api_tokens AS t
        JOIN local_workspace AS w ON w.shop_id = t.shop_id
        JOIN users AS u ON u.shop_id = t.shop_id AND u.id = t.user_id
        WHERE t.token_hash = ? AND t.revoked_at IS NULL
          AND u.active = 1 AND u.revision = t.user_revision
        """, (token_hash,),
    ).fetchone()
    return TokenIdentity(*row) if row else None


def credential_for_mutation(path: Path, session: str, now: int) -> Credential | None:
    with closing(connect(path, "ro")) as db:
        return session_credential(db, session, now)


@contextmanager
def token_write(path: Path, session: str, credential: Credential, now: int):
    with closing(connect(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if session_credential(db, session, now) != credential:
            raise PermissionError("Your session or account changed. Sign in again.")
        yield db


@contextmanager
def token_read(path: Path, session: str, now: int):
    with closing(connect(path, "ro")) as db:
        db.execute("BEGIN")
        credential = session_credential(db, session, now)
        if credential is None:
            raise PermissionError("Sign in to manage API tokens.")
        yield db, credential


def list_owned(db: sqlite3.Connection, credential: Credential, before: int, limit: int) -> list[NamedToken]:
    actor = credential.principal
    rows = db.execute(
        """
        SELECT id, name, scope, revision, kind, preset FROM api_tokens
        WHERE shop_id = ? AND user_id = ? AND revoked_at IS NULL AND user_revision = ?
          AND (? = 0 OR id < ?)
        ORDER BY id DESC LIMIT ?
        """, (actor.shop_id, actor.user_id, credential.revision, before, before, limit),
    ).fetchall()
    return [NamedToken(*row) for row in rows]


def owned_token(db: sqlite3.Connection, credential: Credential, token_id: int) -> NamedToken:
    actor = credential.principal
    row = db.execute(
        """
        SELECT id, name, scope, revision, kind, preset FROM api_tokens
        WHERE shop_id = ? AND user_id = ? AND id = ? AND revoked_at IS NULL
          AND user_revision = ?
        """, (actor.shop_id, actor.user_id, token_id, credential.revision),
    ).fetchone()
    if row is None:
        raise TokenMissing("Token not found.")
    return NamedToken(*row)


def audit_token(db: sqlite3.Connection, actor: Principal, operation: str, token_id: int,
                changes: dict) -> None:
    db.execute(
        """
        INSERT INTO audit_events
            (shop_id, actor_kind, actor_user_id, actor_username, operation,
             source, entity_type, entity_id, changes)
        VALUES (?, 'user', ?, ?, ?, 'web', 'api_token', ?, ?)
        """, (actor.shop_id, actor.user_id, actor.username, operation, token_id,
              json.dumps(changes)),
    )


def issue(db: sqlite3.Connection, credential: Credential, *, key: str, name: str, scope: str,
          token_hash: str, kind: str = "user", preset: str | None = None) -> bool:
    actor = credential.principal
    previous = db.execute(
        """
        SELECT name, scope, kind, preset FROM api_tokens
        WHERE shop_id = ? AND user_id = ? AND create_key = ?
        """, (actor.shop_id, actor.user_id, key),
    ).fetchone()
    if previous is not None:
        if previous != (name, scope, kind, preset):
            raise TokenConflict("This request key already belongs to a different token.")
        return False
    (token_id,) = db.execute(
        """
        INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, scope, create_key, kind,
                               preset)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """, (actor.shop_id, actor.user_id, credential.revision, token_hash, name, scope, key, kind,
               preset),
    ).fetchone()
    audit_token(db, actor, "token.create", token_id,
                {"name": name, "scope": scope, "kind": kind, **({"preset": preset} if preset else {})})
    return True


def revoke(db: sqlite3.Connection, actor: Principal, *, key: str, token_id: int,
           revision: int, now: int) -> None:
    previous = db.execute(
        """
        SELECT id, revision FROM api_tokens WHERE shop_id = ? AND user_id = ? AND revoke_key = ?
        """, (actor.shop_id, actor.user_id, key),
    ).fetchone()
    if previous is not None:
        if previous != (token_id, revision + 1):
            raise TokenConflict("This request key already belongs to a different token revision.")
        return
    row = db.execute(
        """
        UPDATE api_tokens SET revision = revision + 1, revoked_at = ?, revoke_key = ?
        WHERE shop_id = ? AND user_id = ? AND id = ? AND revision = ? AND revoked_at IS NULL
        RETURNING id
        """, (now, key, actor.shop_id, actor.user_id, token_id, revision),
    ).fetchone()
    if row is None:
        existing = db.execute(
            "SELECT 1 FROM api_tokens WHERE shop_id = ? AND user_id = ? AND id = ?",
            (actor.shop_id, actor.user_id, token_id),
        ).fetchone()
        if existing is None:
            raise TokenMissing("Token not found.")
        raise TokenConflict("Token status changed. Open the token list to review it.")
    audit_token(db, actor, "token.revoke", token_id, {"revoked": True})


def audit_preset_refusal(path: Path, identity: TokenIdentity, changes: dict) -> None:
    """Record a refused agent request in its own transaction; the refused one rolled back."""
    with closing(connect(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO audit_events
                (shop_id, actor_kind, actor_user_id, actor_username, operation,
                 source, changes, token_label)
            VALUES (?, 'user', ?, ?, 'token.preset_refused', 'api', ?, ?)
            """, (identity.shop_id, identity.user_id, identity.username, json.dumps(changes),
                  identity.token_name),
        )
