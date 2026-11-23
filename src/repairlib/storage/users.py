"""Shop-scoped user administration and maintenance recovery storage."""

import json
from pathlib import Path
import sqlite3

from repairlib.contracts import User
from repairlib.storage import request_ledger
from repairlib.storage.dry_run import transaction
from repairlib.storage.dry_run import transaction

COLUMNS = "id, username, role, active, revision"
# Built-in local actors (created on first CLI/MCP use) carry this non-Argon2
# marker, are hidden from user administration and can never sign in.
SYSTEM_HASH = "!"
LOCAL_ACTORS = ("cli", "mcp")


class UsernameConflict(Exception):
    """The normalized username is already used in this shop."""


def record(row) -> User:
    return User(row["id"], row["username"], row["role"], bool(row["active"]), row["revision"])


def list_page(db, shop_id: int, before: int | None, limit: int) -> list[User]:
    rows = db.execute(
        f"""
        SELECT {COLUMNS} FROM users
        WHERE shop_id = ? AND (? IS NULL OR id < ?) AND password_hash <> ?
        ORDER BY id DESC LIMIT ?
        """, (shop_id, before, before, SYSTEM_HASH, limit),
    )
    return [record(row) for row in rows]


def find(db, shop_id: int, user_id: int) -> User | None:
    row = db.execute(
        f"SELECT {COLUMNS} FROM users WHERE shop_id = ? AND id = ? AND password_hash <> ?",
        (shop_id, user_id, SYSTEM_HASH),
    ).fetchone()
    return record(row) if row is not None else None


def insert(db, shop_id: int, username: str, role: str, password_hash: str) -> User:
    try:
        row = db.execute(
            f"""
            INSERT INTO users(shop_id, username, role, password_hash)
            VALUES (?, ?, ?, ?) RETURNING {COLUMNS}
            """, (shop_id, username, role, password_hash),
        ).fetchone()
    except sqlite3.IntegrityError as error:
        raise UsernameConflict("This username is already used in the shop.") from error
    return record(row)


def update(db, shop_id: int, user_id: int, revision: int, *, username: str, role: str,
           active: bool, password_hash: str) -> User | None:
    try:
        row = db.execute(
            f"""
            UPDATE users
            SET username = ?, role = ?, active = ?, password_hash = ?, revision = revision + 1
            WHERE shop_id = ? AND id = ? AND revision = ?
            RETURNING {COLUMNS}
            """, (username, role, int(active), password_hash, shop_id, user_id, revision),
        ).fetchone()
    except sqlite3.IntegrityError as error:
        raise UsernameConflict("This username is already used in the shop.") from error
    return record(row) if row is not None else None


def password_hash(db, shop_id: int, user_id: int) -> str:
    return db.execute(
        "SELECT password_hash FROM users WHERE shop_id = ? AND id = ?",
        (shop_id, user_id),
    ).fetchone()[0]


def other_active_admins(db, shop_id: int, user_id: int) -> int:
    return db.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE shop_id = ? AND id <> ? AND role = 'admin' AND active = 1 AND password_hash <> ?
        """, (shop_id, user_id, SYSTEM_HASH),
    ).fetchone()[0]


def save_result(db, shop_id: int, actor_id: int, operation: str, key: str,
                fingerprint: str, user: User) -> None:
    request_ledger.store(db, "user", shop_id, actor_id, operation, key, fingerprint, user,
                         link=user.id)



class LocalActorConflict(Exception):
    """A person account already uses a local actor's name."""


def local_actor_role(path: Path, username: str) -> str | None:
    """Read a local actor's role without creating it; None until its first use."""
    with transaction(path, write=False) as db:
        row = db.execute(
            "SELECT u.role, u.password_hash FROM local_workspace AS w JOIN users AS u "
            "ON u.shop_id = w.shop_id AND u.username = ? WHERE w.id = 1", (username,),
        ).fetchone()
    if row is None:
        return None
    if row["password_hash"] != SYSTEM_HASH:
        raise LocalActorConflict(f"A person account is named {username!r}. Rename it first.")
    return row["role"]


def ensure_local_actor(path: Path, username: str) -> None:
    """Create the shop's built-in `cli` or `mcp` administrator if it does not exist yet."""
    with transaction(path, write=False) as db:
        row = db.execute(
            "SELECT u.password_hash FROM local_workspace AS w JOIN users AS u "
            "ON u.shop_id = w.shop_id AND u.username = ? WHERE w.id = 1", (username,),
        ).fetchone()
    if row is not None and row[0] == SYSTEM_HASH:
        return
    with transaction(path, write=True) as db:
        (shop_id,) = db.execute("SELECT shop_id FROM local_workspace WHERE id = 1").fetchone()
        row = db.execute("SELECT id, password_hash FROM users WHERE shop_id = ? AND username = ?",
                         (shop_id, username)).fetchone()
        if row is not None:
            if row["password_hash"] != SYSTEM_HASH:
                raise LocalActorConflict(f"A person account is named {username!r}. Rename it first.")
            return
        (user_id,) = db.execute(
            "INSERT INTO users(shop_id, username, role, password_hash) VALUES (?, ?, 'admin', ?) RETURNING id",
            (shop_id, username, SYSTEM_HASH),
        ).fetchone()
        db.execute(
            """
            INSERT INTO audit_events(shop_id, actor_kind, operation, source, entity_type, entity_id, changes)
            VALUES (?, 'maintenance', 'user.local_actor', ?, 'user', ?, ?)
            """, (shop_id, username, user_id, json.dumps({"username": username, "role": "admin"})),
        )


def find_by_username(db, shop_id: int, username: str) -> User | None:
    row = db.execute(
        f"SELECT {COLUMNS} FROM users WHERE shop_id = ? AND username = ? AND password_hash <> ?",
        (shop_id, username, SYSTEM_HASH),
    ).fetchone()
    return record(row) if row is not None else None


def set_password(db, shop_id: int, user_id: int, revision: int, new_password_hash: str) -> User | None:
    """Replace a password and advance the revision, which ends that user's sessions and tokens."""
    row = db.execute(
        f"""
        UPDATE users SET password_hash = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? AND password_hash <> ?
        RETURNING {COLUMNS}
        """, (new_password_hash, shop_id, user_id, revision, SYSTEM_HASH),
    ).fetchone()
    return record(row) if row is not None else None
