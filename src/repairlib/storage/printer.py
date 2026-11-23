"""Shop printer configuration and durable single-attempt print requests."""

from dataclasses import asdict
import json
import sqlite3

from repairlib.storage.repairs import audit
from repairlib.storage.dry_run import transaction
from repairlib.storage import request_ledger


def configuration(db, shop_id: int):
    return db.execute(
        "SELECT printer_driver, printer_revision FROM shop_settings WHERE shop_id = ?", (shop_id,),
    ).fetchone()


def configure(db, shop_id: int, driver: str, revision: int) -> None:
    db.execute("""
        UPDATE shop_settings SET printer_driver = ?, printer_revision = printer_revision + 1
        WHERE shop_id = ? AND printer_revision = ?
        """, (driver, shop_id, revision))


def reserve(db, principal, operation: str, key: str, fingerprint: str, repair_id, result) -> None:
    request_ledger.store(db, "printer", principal.shop_id, principal.user_id, operation, key,
                         fingerprint, result, link=repair_id)


def complete(path, principal, source, token_label, operation, key, entity_type, entity_id,
             receipt) -> bool:
    try:
        with transaction(path, write=True) as db:
            db.execute("""
                UPDATE printer_requests SET result = ?
                WHERE shop_id = ? AND actor_id = ? AND operation = ? AND request_key = ?
                """, (json.dumps(asdict(receipt)), principal.shop_id, principal.user_id,
                      operation, key))
            audit(db, principal, source, token_label, operation, entity_type, entity_id,
                  {"driver": "p15", "outcome": receipt.outcome})
    except (sqlite3.Error, OSError):
        return False
    return True
