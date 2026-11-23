"""Shop-scoped customer storage and atomic mutation bookkeeping."""

from dataclasses import asdict
import json

from repairlib.contracts import Customer, CustomerHistoryEvent, CustomerInput, CustomerListRow, OPEN_REPAIR_STAGES
from repairlib.storage import request_ledger

COLUMNS = "id, revision, created_at, name, phone, email, address, notes, archived_at"


def list_page(db, shop_id: int, before: int | None, limit: int, pattern: str | None = None,
              *, include_archived: bool = False, phone_pattern: str | None = None) -> list[CustomerListRow]:
    stages = ", ".join("?" for _ in OPEN_REPAIR_STAGES)
    rows = db.execute(
        f"""
        SELECT {COLUMNS}, (
            SELECT COUNT(*) FROM repairs AS r
            WHERE r.shop_id = customers.shop_id AND r.customer_id = customers.id
                AND r.archived_at IS NULL AND r.stage IN ({stages})
        ) AS open_repair_count
        FROM customers
        WHERE shop_id = ? AND (? IS NULL OR id < ?) AND (? OR archived_at IS NULL)
            AND (? IS NULL OR rl_fold(name || ' ' || phone || ' ' || email) LIKE ? ESCAPE '\\'
                 OR phone_digits LIKE ?)
        ORDER BY id DESC LIMIT ?
        """, (*OPEN_REPAIR_STAGES, shop_id, before, before, include_archived, pattern, pattern,
              phone_pattern, limit),
    )
    return [CustomerListRow(**row) for row in rows]


def possible_duplicate_id(db, shop_id: int, digits: str, email: str) -> int | None:
    if not digits and not email:
        return None
    row = db.execute(
        """
        SELECT MIN(id) FROM (
            SELECT id FROM customers WHERE shop_id = ? AND phone_digits = ?
            UNION ALL
            SELECT id FROM customers WHERE shop_id = ? AND trim(email) = ? COLLATE NOCASE
        )
        """, (shop_id, digits or None, shop_id, email or None),
    ).fetchone()
    return row[0]


def insert(db, shop_id: int, details: CustomerInput) -> Customer:
    row = db.execute(
        f"""
        INSERT INTO customers(shop_id, name, phone, email, address, notes)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING {COLUMNS}
        """, (shop_id, details.name, details.phone, details.email, details.address, details.notes),
    ).fetchone()
    return Customer(**row)


def previous_request(db, shop_id: int, actor_id: int, operation: str, key: str):
    return request_ledger.find(db, "customer", shop_id, actor_id, operation, key)


def save_result(db, shop_id: int, actor_id: int, operation: str, key: str,
                fingerprint: str, customer: Customer) -> None:
    request_ledger.store(db, "customer", shop_id, actor_id, operation, key, fingerprint, customer,
                         link=customer.id)



def find(db, shop_id: int, customer_id: int) -> Customer | None:
    row = db.execute(
        f"SELECT {COLUMNS} FROM customers WHERE shop_id = ? AND id = ?",
        (shop_id, customer_id),
    ).fetchone()
    return Customer(**row) if row is not None else None


def update(db, shop_id: int, customer_id: int, revision: int, values: CustomerInput) -> Customer | None:
    row = db.execute(
        f"""
        UPDATE customers
        SET name = ?, phone = ?, email = ?, address = ?, notes = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ?
        RETURNING {COLUMNS}
        """, (values.name, values.phone, values.email, values.address, values.notes,
              shop_id, customer_id, revision),
    ).fetchone()
    return Customer(**row) if row is not None else None


def set_archived_at(db, shop_id: int, customer_id: int, revision: int, timestamp: str | None) -> Customer:
    row = db.execute(
        f"""
        UPDATE customers SET archived_at = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ?
        RETURNING {COLUMNS}
        """, (timestamp, shop_id, customer_id, revision),
    ).fetchone()
    return Customer(**row)


def history_page(db, shop_id: int, customer_id: int, before: int | None,
                 limit: int) -> list[CustomerHistoryEvent]:
    rows = db.execute(
        """
        SELECT id, operation, occurred_at, actor_user_id, actor_username, source, token_label, actor_kind,
               changes
        FROM audit_events
        WHERE shop_id = ? AND entity_type = 'customer' AND entity_id = ?
          AND (? IS NULL OR id < ?)
        ORDER BY id DESC LIMIT ?
        """, (shop_id, customer_id, before, before, limit),
    )
    return [CustomerHistoryEvent(**(dict(row) | {"changes": json.loads(row["changes"])})) for row in rows]


def intake_identities(db, shop_id: int):
    """Include archived identities when screening CSV creates."""
    return db.execute("SELECT name, phone, email FROM customers WHERE shop_id = ?", (shop_id,))


def is_anonymized(db, shop_id: int, customer_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM audit_events WHERE shop_id = ? AND entity_type = 'customer' AND entity_id = ? "
        "AND operation = 'customer.anonymize'", (shop_id, customer_id)).fetchone() is not None


def erase_personal_data(db, shop_id: int, customer_id: int, revision: int, name: str) -> Customer | None:
    """Replace the personal columns with neutral values; ids, links and the archive state stay."""
    row = db.execute(
        f"""
        UPDATE customers
        SET name = ?, phone = '', email = '', address = '', notes = '', revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ?
        RETURNING {COLUMNS}
        """, (name, shop_id, customer_id, revision),
    ).fetchone()
    return Customer(**row) if row is not None else None


def redact_history(db, shop_id: int, customer_id: int, placeholder: str) -> None:
    """Keep every customer audit row but blank the before/after values of field changes."""
    rows = db.execute(
        "SELECT id, changes FROM audit_events WHERE shop_id = ? AND entity_type = 'customer' "
        "AND entity_id = ? AND operation = 'customer.update'", (shop_id, customer_id)).fetchall()
    for row in rows:
        redacted = {name: {"before": placeholder, "after": placeholder}
                    for name in json.loads(row["changes"])}
        db.execute("UPDATE audit_events SET changes = ? WHERE id = ?", (json.dumps(redacted), row["id"]))


def redact_requests(db, shop_id: int, customer_id: int, fingerprint: str, customer: Customer) -> None:
    """Replace stored retry receipts, which hold the old values and a hash of them."""
    db.execute(
        "UPDATE customer_requests SET fingerprint = ?, result = ? WHERE shop_id = ? AND customer_id = ?",
        (fingerprint, json.dumps(asdict(customer)), shop_id, customer_id))
