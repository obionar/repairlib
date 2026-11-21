"""Replay receipts for mutations: one lookup/insert helper over a static table allowlist.

Every request table, scope column and link column comes from FAMILIES below. Callers pass a
family name, never a table name, so no SQL here is built from caller input.
"""

from dataclasses import asdict
import json
from typing import NamedTuple


class UnknownFamily(ValueError):
    """The operation family is not in the allowlist."""


class Family(NamedTuple):
    table: str
    scope: str | None  # Column holding the operation (or CSV kind); None when one operation exists.
    link: str | None   # Column holding the id of the record the receipt produced, if any.


FAMILIES = {
    "attachment": Family("attachment_requests", "operation", "attachment_id"),
    "charge": Family("charge_requests", "operation", "repair_id"),
    "csv": Family("csv_requests", "kind", None),
    "customer": Family("customer_requests", "operation", "customer_id"),
    "inventory": Family("inventory_requests", "operation", "item_id"),
    "printer": Family("printer_requests", "operation", "repair_id"),
    "repair": Family("repair_requests", "operation", "repair_id"),
    "settings": Family("settings_requests", None, None),
    "stock": Family("stock_requests", "operation", "movement_id"),
    "user": Family("user_requests", "operation", "user_id"),
}


def family(name: str) -> Family:
    if not isinstance(name, str) or name not in FAMILIES:
        raise UnknownFamily("Unknown request family.")
    return FAMILIES[name]


def find(db, name: str, shop_id: int, actor_id: int, operation: str | None, key: str):
    """Return the stored `(fingerprint, result)` row for this request, or None."""
    spec = family(name)
    table = spec.table
    scope = f" AND {spec.scope} = ?" if spec.scope else ""
    values = (shop_id, actor_id, *((operation,) if spec.scope else ()), key)
    return db.execute(
        f"SELECT fingerprint, result FROM {table} "
        f"WHERE shop_id = ? AND actor_id = ?{scope} AND request_key = ?", values).fetchone()


def store(db, name: str, shop_id: int, actor_id: int, operation: str | None, key: str,
          fingerprint: str, result, *, link: int | None = None) -> None:
    """Record the result of a mutation. `result` is a dataclass, stored as JSON."""
    spec = family(name)
    table = spec.table
    columns = ["shop_id", "actor_id"]
    values = [shop_id, actor_id]
    if spec.scope:
        columns.append(spec.scope)
        values.append(operation)
    columns += ["request_key", "fingerprint"]
    values += [key, fingerprint]
    if spec.link:
        link_column = spec.link
        columns.append(link_column)
        values.append(link)
    columns.append("result")
    values.append(json.dumps(asdict(result)))
    marks = ", ".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", values)
