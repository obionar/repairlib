"""Anonymize one customer and export one customer's own records."""

from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from repairlib.contracts import Customer, CustomerAnonymize, CustomerExport, RepairExport
from repairlib.domain.charges import visible
from repairlib.domain.customers import (
    CustomerAccess, CustomerError, MAX_ID, authorized, find_customer, replay,
)
from repairlib.storage import charges, customers, repairs

NEUTRAL_NAME = "Anonymized customer"
REDACTED = "[erased]"
ERASED_FIELDS = ["name", "phone", "email", "address", "notes"]
RECEIPT_FINGERPRINT = sha256(b"customer.anonymized").hexdigest()


def anonymize_customer(workspace: Path, access: CustomerAccess, customer_id: int,
                       change: CustomerAnonymize, *, key: str) -> Customer:
    operation = "customer.anonymize"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        if principal.role != "admin":
            raise CustomerError(403, "forbidden", "Only an administrator can anonymize a customer.")
        current = find_customer(db, principal.shop_id, customer_id)
        if not 1 <= change.revision <= MAX_ID:
            raise CustomerError(422, "validation", "Supply the reviewed customer revision.")
        fingerprint, previous = replay(db, principal, operation, key, {"id": customer_id, **asdict(change)})
        if previous is not None:
            return previous
        if customers.is_anonymized(db, principal.shop_id, customer_id):
            raise CustomerError(409, "conflict", "The customer is already anonymized.")
        if current.revision != change.revision or current.revision == MAX_ID:
            raise CustomerError(409, "stale_revision", "The customer changed. Review the latest version.")
        updated = customers.erase_personal_data(db, principal.shop_id, customer_id, change.revision,
                                                NEUTRAL_NAME)
        if updated is None:
            raise CustomerError(409, "stale_revision", "The customer changed. Review the latest version.")
        customers.redact_history(db, principal.shop_id, customer_id, REDACTED)
        customers.redact_requests(db, principal.shop_id, customer_id, RECEIPT_FINGERPRINT, updated)
        customers.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint, updated)
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        customers.audit(db, principal.shop_id, principal.user_id, principal.username, access.source,
                        token_label, operation, customer_id,
                        {"anonymized_at": stamp, "erased_fields": ERASED_FIELDS})
        return updated


def export_customer(workspace: Path, access: CustomerAccess, customer_id: int) -> CustomerExport:
    """Read-only; authorized exactly like `read_customer`."""
    with authorized(workspace, access) as (db, principal, _, _):
        record = find_customer(db, principal.shop_id, customer_id)
        items = [RepairExport(**asdict(repair), charges=[
            visible(charge, principal) for charge in charges.list_for_repair(db, principal.shop_id, repair.id)])
            for repair in repairs.for_customer(db, principal.shop_id, customer_id)]
        return CustomerExport(record, items)
