"""Shop-scoped credential reads and atomic session changes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from repairlib.search import fold

SQLITE_SYNCHRONOUS = "FULL"
SQLITE_BUSY_TIMEOUT_MS = 5000
# A shared shop PC must not keep an abandoned sign-in usable.
SESSION_IDLE_SECONDS = 60 * 60
TOUCH_SECONDS = 60  # idle expiry is hourly; minute precision avoids a disk write per request


@dataclass(frozen=True)
class Principal:
    shop_id: int
    user_id: int
    username: str
    role: str
    shop_name: str


@dataclass(frozen=True)
class Credential:
    principal: Principal
    revision: int
    password_hash: str


def connect(path: Path, mode: str) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True,
                         timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    db.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS}")
    db.execute("PRAGMA foreign_keys = ON")
    db.create_function("rl_fold", 1, fold, deterministic=True)
    return db


def lookup_credential(path: Path, username: str) -> Credential | None:
    with closing(connect(path, "ro")) as db:
        row = db.execute(
            """
            SELECT u.shop_id, u.id, u.username, u.role, s.name, u.revision, u.password_hash
            FROM local_workspace AS w JOIN users AS u ON u.shop_id = w.shop_id
            JOIN shops AS s ON s.id = u.shop_id
            WHERE u.username = ? AND u.active = 1
            """, (username,),
        ).fetchone()
    if row is None:
        return None
    shop_id, user_id, name, role, shop_name, revision, password_hash = row
    return Credential(Principal(shop_id, user_id, name, role, shop_name), revision, password_hash)


def issue_session(path: Path, credential: Credential, token_hash: str, now: int,
                  expires_at: int) -> bool:
    actor = credential.principal
    with closing(connect(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        issued = db.execute(
            """
            INSERT INTO sessions(token_hash, shop_id, user_id, user_revision, expires_at, last_seen_at)
            SELECT ?, u.shop_id, u.id, u.revision, ?, ?
            FROM local_workspace AS w JOIN users AS u ON u.shop_id = w.shop_id
            WHERE u.shop_id = ? AND u.id = ? AND u.active = 1
              AND u.revision = ? AND u.password_hash = ?
            """,
            (token_hash, expires_at, now, actor.shop_id, actor.user_id,
             credential.revision, credential.password_hash),
        )
        if not issued.rowcount:
            return False
        db.execute(
            "INSERT INTO audit_events(shop_id, actor_kind, actor_user_id, operation) "
            "VALUES (?, 'user', ?, 'login')", (actor.shop_id, actor.user_id),
        )
    return True


def active_count(db: sqlite3.Connection, shop_id: int, now: int) -> int:
    """Sessions still open for the shop at `now`."""
    # Same lifetime, idle, active-user and revision rules as `session_credential`.
    return db.execute(
        """
        SELECT COUNT(*) FROM sessions AS s
        JOIN users AS u ON u.shop_id = s.shop_id AND u.id = s.user_id
        WHERE s.shop_id = ? AND s.expires_at > ? AND s.last_seen_at > ?
          AND u.active = 1 AND u.revision = s.user_revision
        """, (shop_id, now, now - SESSION_IDLE_SECONDS)).fetchone()[0]


def session_credential(db: sqlite3.Connection, token_hash: str, now: int) -> Credential | None:
    row = db.execute(
        """
        SELECT u.shop_id, u.id, u.username, u.role, shop.name, u.revision, u.password_hash
        FROM sessions AS s
        JOIN local_workspace AS w ON w.shop_id = s.shop_id
        JOIN users AS u ON u.shop_id = s.shop_id AND u.id = s.user_id
        JOIN shops AS shop ON shop.id = u.shop_id
        WHERE s.token_hash = ? AND s.expires_at > ? AND s.last_seen_at > ?
          AND u.active = 1 AND u.revision = s.user_revision
        """, (token_hash, now, now - SESSION_IDLE_SECONDS),
    ).fetchone()
    if row is None:
        return None
    shop_id, user_id, name, role, shop_name, revision, password_hash = row
    return Credential(Principal(shop_id, user_id, name, role, shop_name), revision, password_hash)


def local_principal(db: sqlite3.Connection, username: str) -> Principal | None:
    """Resolve a built-in local actor (`cli` or `mcp`) of this workspace's shop."""
    row = db.execute(
        """
        SELECT u.shop_id, u.id, u.username, u.role, shop.name
        FROM local_workspace AS w
        JOIN users AS u ON u.shop_id = w.shop_id
        JOIN shops AS shop ON shop.id = u.shop_id
        WHERE w.id = 1 AND u.username = ? AND u.password_hash = '!' AND u.active = 1
        """, (username,),
    ).fetchone()
    return Principal(*row) if row else None


def session_principal(path: Path, token_hash: str, now: int) -> Principal | None:
    """Resolve a session and record the activity that keeps it from idling out."""
    with closing(connect(path, "rw")) as db, db:
        credential = session_credential(db, token_hash, now)
        if credential is None:
            return None
        seen = db.execute("SELECT last_seen_at FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        if seen and seen[0] > now - TOUCH_SECONDS:
            return credential.principal  # recent enough: a read must not take the write lock
        db.execute("BEGIN IMMEDIATE")
        credential = session_credential(db, token_hash, now)  # recheck under the lock
        if credential is None:
            return None
        db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))
        return credential.principal


def revoke_session(path: Path, token_hash: str, now: int) -> None:
    with closing(connect(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            DELETE FROM sessions WHERE token_hash = ? AND expires_at > ?
              AND shop_id = (SELECT shop_id FROM local_workspace WHERE id = 1)
              AND EXISTS (
                SELECT 1 FROM users
                WHERE users.shop_id = sessions.shop_id AND users.id = sessions.user_id
                  AND users.active = 1 AND users.revision = sessions.user_revision
              )
            RETURNING shop_id, user_id
            """, (token_hash, now),
        ).fetchone()
        if row:
            db.execute(
                "INSERT INTO audit_events(shop_id, actor_kind, actor_user_id, operation) "
                "VALUES (?, 'user', ?, 'logout')", row,
            )
