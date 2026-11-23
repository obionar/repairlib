"""Administrator-only user changes and local-shell password resets."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from repairlib.contracts import User, UserInput, UserPage, UserPatch
from repairlib.domain.customers import (
    CustomerAccess, CustomerError, audited_preset_refusal, check_credential, resolve_principal,
)
from repairlib.domain.passwords import hash_with_salt, hasher
from repairlib.domain.replay import KEY_FORMAT, replay as replay_receipt
from repairlib.paths import database_path
from repairlib.storage import dry_run, users

MAX_ID = (1 << 63) - 1
ROLES = {"admin", "technician", "viewer"}


class UserError(CustomerError):
    """A stable user-administration refusal for web and JSON adapters."""


UserAccess = CustomerAccess


@contextmanager
def authorized(workspace: Path, access: UserAccess, *, write: bool = False,
               capability: str = "manage users"):
    check_credential(access, UserError)
    with audited_preset_refusal(workspace), dry_run.transaction(database_path(workspace), write=write) as db:
        principal, token_label, scope = resolve_principal(db, access, UserError, "users", write)
        if principal.role != "admin":
            raise UserError(403, "forbidden", f"Only an administrator can {capability}.")
        if write and scope != "write":
            raise UserError(403, "forbidden", "A write-scoped administrator token is required.")
        yield db, principal, token_label


def preauthorize_write(workspace: Path, access: UserAccess) -> None:
    """Reject unauthorized password work before hashing; writes recheck in their transaction."""
    with authorized(workspace, access, write=True):
        pass


def validate_key(key: str) -> None:
    if not KEY_FORMAT.fullmatch(key):
        raise UserError(422, "validation", "Supply a valid idempotency key.")


def validate_password(password: str) -> None:
    if not 1 <= len(password) <= 1024:
        raise UserError(422, "validation", "Enter a password of at most 1024 characters.",
                        {"password": "Enter a password of 1 to 1024 characters."})


def request_password_hash(password: str, key: str, context: str) -> str:
    validate_password(password)
    # A unique request key provides a stable Argon2 salt, so an identical retry
    # has the same fingerprint without storing a fast digest of the password.
    salt = sha256(f"repairlib-user-password\0{key}\0{context}".encode()).digest()[:16]
    return hash_with_salt(password, salt)


def normalize_username(username: str) -> str:
    username = username.strip()
    if not 1 <= len(username) <= 80 or not username.isprintable():
        raise UserError(422, "validation", "Check the user fields.",
                        {"username": "Enter 1 to 80 printable characters."})
    if username in users.LOCAL_ACTORS:
        raise UserError(422, "validation", "Check the user fields.",
                        {"username": "This name is reserved for local tools."})
    return username


def normalize_role(role: str) -> str:
    if role not in ROLES:
        raise UserError(422, "validation", "Check the user fields.", {"role": "Choose a valid role."})
    return role


def replay(db, principal, operation: str, key: str, payload: dict):
    return replay_receipt(db, principal, "user", operation, key, payload, lambda values: User(**values), UserError)


def create_user(workspace: Path, access: UserAccess, details: UserInput, *, key: str) -> User:
    validate_key(key)
    username = normalize_username(details.username)
    role = normalize_role(details.role)
    preauthorize_write(workspace, access)
    password_hash = request_password_hash(details.password, key, f"create:{username}")
    operation = "user.create"
    with authorized(workspace, access, write=True) as (db, principal, token_label):
        values = {"username": username, "role": role, "active": True, "password_hash": password_hash}
        fingerprint, previous = replay(db, principal, operation, key, values)
        if previous is not None:
            return previous
        try:
            created = users.insert(db, principal.shop_id, username, role, password_hash)
        except users.UsernameConflict as error:
            raise UserError(409, "conflict", str(error), {"username": str(error)}) from None
        users.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint, created)
        users.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                    token_label, operation, created.id,
                    {"username": username, "role": role, "active": True})
        return created


def find_user(db, shop_id: int, user_id: int) -> User:
    record = users.find(db, shop_id, user_id) if 1 <= user_id <= MAX_ID else None
    if record is None:
        raise UserError(404, "not_found", "User not found.")
    return record


def list_users(workspace: Path, access: UserAccess, *, before: int | None = None,
               limit: int = 25) -> UserPage:
    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
        raise UserError(422, "validation", "Invalid user page.")
    with authorized(workspace, access) as (db, principal, _):
        rows = users.list_page(db, principal.shop_id, before, limit + 1)
    page = rows[:limit]
    return UserPage(page, page[-1].id if len(rows) > limit else None)


def read_user(workspace: Path, access: UserAccess, user_id: int) -> User:
    with authorized(workspace, access) as (db, principal, _):
        return find_user(db, principal.shop_id, user_id)


def update_user(workspace: Path, access: UserAccess, user_id: int, patch: UserPatch, *,
                key: str) -> User:
    validate_key(key)
    if not 1 <= patch.revision < MAX_ID:
        raise UserError(422, "validation", "Supply the reviewed user revision.")
    values = {}
    if patch.username is not None:
        values["username"] = normalize_username(patch.username)
    if patch.role is not None:
        values["role"] = normalize_role(patch.role)
    if patch.active is not None:
        values["active"] = patch.active
    if patch.password is not None:
        preauthorize_write(workspace, access)
        values["password_hash"] = request_password_hash(
            patch.password, key, f"update:{user_id}:{patch.revision}",
        )
    operation = "user.update"
    with authorized(workspace, access, write=True) as (db, principal, token_label):
        current = find_user(db, principal.shop_id, user_id)
        payload = {"id": user_id, "revision": patch.revision, "fields": values}
        fingerprint, previous = replay(db, principal, operation, key, payload)
        if previous is not None:
            return previous
        if current.revision != patch.revision:
            raise UserError(409, "stale_revision", "The user changed. Review the latest version.")
        changes = {}
        for name in ("username", "role", "active"):
            if name in values and values[name] != getattr(current, name):
                changes[name] = {"before": getattr(current, name), "after": values[name]}
        if "password_hash" in values:
            changes["password"] = {"changed": True}
        if not changes:
            raise UserError(422, "validation", "Change at least one user field.")
        final_role = values.get("role", current.role)
        final_active = values.get("active", current.active)
        if (current.role == "admin" and current.active
                and (final_role != "admin" or not final_active)
                and not users.other_active_admins(db, principal.shop_id, user_id)):
            raise UserError(409, "last_admin", "Cannot disable or demote the last active administrator.")
        try:
            updated = users.update(
                db, principal.shop_id, user_id, patch.revision,
                username=values.get("username", current.username),
                role=final_role, active=final_active,
                password_hash=values.get("password_hash") or users.password_hash(
                    db, principal.shop_id, user_id,
                ),
            )
        except users.UsernameConflict as error:
            raise UserError(409, "conflict", str(error), {"username": str(error)}) from None
        if updated is None:
            raise UserError(409, "stale_revision", "The user changed. Review the latest version.")
        users.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint, updated)
        users.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                    token_label, operation, user_id, changes)
        return updated


def ensure_local_actor(workspace: Path, source: str) -> None:
    """Make sure the built-in `cli` or `mcp` administrator exists for this workspace's shop."""
    if source not in users.LOCAL_ACTORS:
        raise ValueError("Unknown local actor.")
    try:
        users.ensure_local_actor(database_path(workspace), source)
    except users.LocalActorConflict as error:
        raise UserError(409, "local_actor_conflict", str(error)) from None


def set_password(workspace: Path, access: UserAccess, username: str, password: str) -> User:
    """Set any person's password from a trusted local shell; ends their sessions and tokens."""
    username = normalize_username(username)
    validate_password(password)
    preauthorize_write(workspace, access)
    password_hash = hasher().hash(password)
    with authorized(workspace, access, write=True) as (db, principal, token_label):
        current = users.find_by_username(db, principal.shop_id, username)
        if current is None:
            raise UserError(404, "not_found", "User not found.")
        updated = users.set_password(db, principal.shop_id, current.id, current.revision, password_hash)
        if updated is None:
            raise UserError(409, "stale_revision", "The user changed. Try again.")
        users.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                    token_label, "user.password", current.id, {"password": {"changed": True}})
        return updated
