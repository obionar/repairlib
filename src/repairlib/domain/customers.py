"""Customer permissions, validation, and transaction boundaries."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from time import time
from typing import Literal

from repairlib.contracts import (
    Customer, CustomerArchiveChange, CustomerHistoryPage, CustomerInput, CustomerPage, CustomerPatch,
    CustomerResponse, CustomerWithNotices,
)
from repairlib.domain.replay import KEY_FORMAT, replay as replay_receipt
from repairlib.domain.sessions import session_hash
from repairlib.domain.tokens import OnVerified, PRESETS, api_token_hash, token_scopes
from repairlib.paths import database_path
from repairlib.search import PHONE_SHAPE, like_pattern, normalize_query, phone_digits, phone_search_pattern
from repairlib.storage import StorageError, StorageOperationalError, customers, dry_run, sessions, tokens

FIELD_LIMITS = {"name": 160, "phone": 80, "email": 254, "address": 500, "notes": 1000}
MAX_ID = (1 << 63) - 1
MIN_PHONE_DIGITS = 6
EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+")
WRITE_ROLES = {"admin", "technician"}


class CustomerError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.fields = fields


LOCAL_SOURCES = ("cli", "mcp")


@dataclass(frozen=True)
class CustomerAccess:
    """Who is asking: a browser session, an API token, or a built-in local actor."""

    source: Literal["web", "api", "cli", "mcp"]
    credential: str = field(default="", repr=False)
    on_verified: OnVerified | None = field(default=None, repr=False, compare=False)


def resolve_principal(db, access: CustomerAccess, error=CustomerError, resource: str = "customers",
                      write: bool = False):
    """Return `(principal, token_label, scope)` for any access source, or raise 401.

    A token with a preset may only write the resources its preset names; anything else is refused
    here, the one place every domain operation passes through, before any work is done.
    """
    if access.source in LOCAL_SOURCES:
        principal = sessions.local_principal(db, access.source)
        if principal is None:
            raise error(401, "unauthenticated", "The local actor is unavailable.")
        return principal, None, "write"
    if access.source == "web":
        session = sessions.session_credential(db, session_hash(access.credential), int(time()))
        if session is None:
            raise error(401, "unauthenticated", f"Sign in to access {resource}.")
        principal, label, scope = session.principal, None, "write"
    else:
        principal = tokens.identity_in(db, api_token_hash(access.credential))
        if principal is None or principal.scope not in token_scopes(principal.role):
            raise error(401, "unauthenticated", "A valid API token is required.")
        label, scope = principal.token_name, principal.scope
        if write and principal.preset is not None and resource not in PRESETS[principal.preset]:
            refusal = error(403, "preset_forbidden",
                            f"The {principal.preset} preset of this token does not allow changing {resource}.")
            refusal.preset_refusal = {"preset": principal.preset, "resource": resource}
            refusal.refused_token = principal
            raise refusal
    if access.on_verified is not None:
        access.on_verified((principal.shop_id, principal.user_id))
    return principal, label, scope


def check_credential(access: CustomerAccess, error=CustomerError) -> None:
    """Refuse a malformed credential before opening the database."""
    if access.source in LOCAL_SOURCES:
        return
    digest = session_hash(access.credential) if access.source == "web" else api_token_hash(access.credential)
    if not digest:
        raise error(401, "unauthenticated", "Sign in or supply a valid API token.")


@contextmanager
def audited_preset_refusal(workspace: Path):
    """Record a preset refusal after its transaction has rolled back (nothing else is written)."""
    try:
        yield
    except Exception as refusal:
        details = getattr(refusal, "preset_refusal", None)
        if details is not None and dry_run.active() is None:
            tokens.audit_preset_refusal(database_path(workspace), refusal.refused_token, details)
        raise


@contextmanager
def authorized(workspace: Path, access: CustomerAccess, *, write: bool = False,
               resource: str = "customers"):
    check_credential(access)
    if write and access.source not in LOCAL_SOURCES and dry_run.active() is None:
        # Refuse an unknown credential in a read transaction, before it can queue for the write lock.
        with customers.transaction(database_path(workspace), write=False) as db:
            resolve_principal(db, access, resource=resource)
    with audited_preset_refusal(workspace), customers.transaction(database_path(workspace), write=write) as db:
        principal, token_label, scope = resolve_principal(db, access, resource=resource, write=write)
        can_write = principal.role in WRITE_ROLES and scope == "write"
        if write and not can_write:
            raise CustomerError(403, "forbidden", f"Your account or token cannot change {resource}.")
        yield db, principal, token_label, can_write


def may_write(workspace: Path, access: CustomerAccess) -> bool:
    with authorized(workspace, access) as (_, _, _, can_write):
        return can_write


def normalize_fields(details: dict[str, str]) -> dict[str, str]:
    errors = {}
    values = {}
    for name, value in details.items():
        value = value.strip().replace("\r\n", "\n")
        values[name] = value
        if name == "name" and not value:
            errors[name] = "Enter a customer name."
        elif len(value) > FIELD_LIMITS[name]:
            errors[name] = f"Use at most {FIELD_LIMITS[name]} characters."
        elif any(not char.isprintable() and (name not in {"address", "notes"} or char not in "\n\t")
                 for char in value):
            errors[name] = "Remove unsupported characters."
    if errors:
        raise CustomerError(422, "validation", "Check the customer fields.", errors)
    return values


def contact_warnings(details: CustomerInput) -> dict[str, str]:
    warnings = {}
    phone, email = details.phone.strip(), details.email.strip()
    if phone and (not PHONE_SHAPE.fullmatch(phone) or len(phone_digits(phone)) < MIN_PHONE_DIGITS):
        warnings["phone"] = "Check the phone number. Use at least six digits."
    if email and not EMAIL_SHAPE.fullmatch(email):
        warnings["email"] = "Check the email address. Use a format like name@example.com."
    return warnings


def with_contact_notices(record: Customer, warnings: dict[str, str],
                         duplicate_id: int | None = None) -> CustomerResponse:
    if warnings or duplicate_id is not None:
        return CustomerWithNotices(**asdict(record), contact_warnings=warnings,
                                   possible_duplicate_id=duplicate_id)
    return record


def receipt_customer(data: dict) -> CustomerResponse:
    if "contact_warnings" in data:
        return CustomerWithNotices(**data)
    return Customer(**data)


def search_pattern(query: str) -> str | None:
    try:
        folded = normalize_query(query)
    except ValueError as error:
        raise CustomerError(422, "validation", str(error), {"q": str(error)}) from None
    return like_pattern(folded) if folded else None


def validate_page(before: int | None, limit: int) -> None:
    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
        raise CustomerError(422, "validation", "Invalid customer page.")


def list_customers(workspace: Path, access: CustomerAccess, *, before: int | None = None, limit: int = 25,
                   query: str = "", include_archived: bool = False):
    pattern = search_pattern(query)
    with authorized(workspace, access) as (db, principal, _, can_write):
        validate_page(before, limit)
        rows = customers.list_page(db, principal.shop_id, before, limit + 1, pattern,
                                   include_archived=include_archived, phone_pattern=phone_search_pattern(query))
    page = rows[:limit]
    return CustomerPage(page, page[-1].id if len(rows) > limit else None), can_write


def replay(db, principal, operation: str, key: str, payload: dict):
    return replay_receipt(db, principal, "customer", operation, key, payload, receipt_customer, CustomerError)


def create_customer(workspace: Path, access: CustomerAccess, details: CustomerInput, *, key: str) -> CustomerResponse:
    operation = "customer.create"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        values = normalize_fields(asdict(details))
        fingerprint, previous = replay(db, principal, operation, key, values)
        if previous is not None:
            return previous
        details = CustomerInput(**values)
        warnings = contact_warnings(details)
        digits = phone_digits(details.phone)
        duplicate = customers.possible_duplicate_id(
            db, principal.shop_id, digits if len(digits) >= MIN_PHONE_DIGITS else "",
            details.email if "email" not in warnings else "",
        )
        customer = with_contact_notices(customers.insert(db, principal.shop_id, details), warnings, duplicate)
        customers.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint, customer)
        customers.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                        token_label, operation, customer.id, {"fields": list(asdict(details))})
        return customer


def read_create_notice(workspace: Path, access: CustomerAccess, customer_id: int,
                       key: str) -> CustomerWithNotices | None:
    if not KEY_FORMAT.fullmatch(key):
        return None
    with authorized(workspace, access) as (db, principal, _, _):
        receipt = customers.previous_request(db, principal.shop_id, principal.user_id, "customer.create", key)
    if receipt is not None:
        record = receipt_customer(json.loads(receipt["result"]))
        if record.id == customer_id and isinstance(record, CustomerWithNotices):
            return record
    return None


def find_customer(db, shop_id: int, customer_id: int) -> Customer:
    record = customers.find(db, shop_id, customer_id) if 1 <= customer_id <= MAX_ID else None
    if record is None:
        raise CustomerError(404, "not_found", "Customer not found.")
    return record


def read_customer(workspace: Path, access: CustomerAccess, customer_id: int):
    with authorized(workspace, access) as (db, principal, _, can_write):
        record = find_customer(db, principal.shop_id, customer_id)
        return with_contact_notices(record, contact_warnings(record)), can_write


def update_customer(workspace: Path, access: CustomerAccess, customer_id: int,
                    patch: CustomerPatch, *, key: str) -> CustomerResponse:
    operation = "customer.update"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        current = find_customer(db, principal.shop_id, customer_id)
        if not 1 <= patch.revision <= MAX_ID:
            raise CustomerError(422, "validation", "Supply the reviewed customer revision.")
        values = normalize_fields({name: value for name in FIELD_LIMITS
                                   if (value := getattr(patch, name)) is not None})
        payload = {"id": customer_id, "revision": patch.revision, "fields": values}
        fingerprint, previous = replay(db, principal, operation, key, payload)
        if previous is not None:
            return previous
        if current.revision != patch.revision or current.revision == MAX_ID:
            raise CustomerError(409, "stale_revision", "The customer changed. Review the latest version.")
        original = {name: getattr(current, name) for name in FIELD_LIMITS}
        changes = {name: {"before": original[name], "after": value}
                   for name, value in values.items() if value != original[name]}
        if not changes:
            raise CustomerError(422, "validation", "Change at least one customer field.")
        updated = customers.update(db, principal.shop_id, customer_id, patch.revision,
                                   CustomerInput(**(original | values)))
        if updated is None:
            raise CustomerError(409, "stale_revision", "The customer changed. Review the latest version.")
        updated = with_contact_notices(updated, contact_warnings(updated))
        customers.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint, updated)
        customers.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                        token_label, operation, customer_id, changes)
        return updated


def change_customer_archive(workspace: Path, access: CustomerAccess, customer_id: int,
                            change: CustomerArchiveChange, *, key: str) -> CustomerResponse:
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        current = find_customer(db, principal.shop_id, customer_id)
        if not 1 <= change.revision <= MAX_ID:
            raise CustomerError(422, "validation", "Supply the reviewed customer revision.")
        payload = {"id": customer_id, **asdict(change)}
        fingerprint, previous = replay(db, principal, "customer.archive", key, payload)
        if previous is not None:
            return previous
        if current.revision != change.revision or current.revision == MAX_ID:
            raise CustomerError(409, "stale_revision", "The customer changed. Review the latest version.")
        if change.enabled == (current.archived_at is not None):
            raise CustomerError(409, "conflict", "The customer already has the requested archive status.")
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if change.enabled else None
        updated = customers.set_archived_at(db, principal.shop_id, customer_id, change.revision, timestamp)
        updated = with_contact_notices(updated, contact_warnings(updated))
        customers.save_result(db, principal.shop_id, principal.user_id, "customer.archive", key, fingerprint, updated)
        operation = "customer.archive" if change.enabled else "customer.restore"
        customers.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                        token_label, operation, customer_id,
                        {"archived_at": {"before": current.archived_at, "after": updated.archived_at}})
        return updated


def read_customer_history(workspace: Path, access: CustomerAccess, customer_id: int,
                          *, before: int | None = None, limit: int = 25):
    with authorized(workspace, access) as (db, principal, _, can_write):
        record = find_customer(db, principal.shop_id, customer_id)
        validate_page(before, limit)
        rows = customers.history_page(db, principal.shop_id, customer_id, before, limit + 1)
    page = rows[:limit]
    return record, CustomerHistoryPage(page, page[-1].id if len(rows) > limit else None), can_write
