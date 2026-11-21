"""Idempotent replay: one receipt lookup for every mutation family.

A retry with the same key and an identical payload returns the stored result; the same
key with a different payload is a conflict. Callers run this before any current-state
guard (archived, deleted, assignment), because replay needs only the request identity.
"""

from hashlib import sha256
import json
import re
from typing import Callable

from repairlib.storage import request_ledger

KEY_FORMAT = re.compile(r"[A-Za-z0-9_-]{1,128}")


def fingerprint_of(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def replay(db, principal, family: str, operation: str | None, key: str, payload: dict,
           shape: Callable[[dict], object], error: Callable[..., Exception]):
    """Return `(fingerprint, previous_result_or_None)`; refuse a bad key or a key reused differently."""
    if not isinstance(key, str) or not KEY_FORMAT.fullmatch(key):
        raise error(422, "validation", "Supply a valid idempotency key.")
    fingerprint = fingerprint_of(payload)
    row = request_ledger.find(db, family, principal.shop_id, principal.user_id, operation, key)
    if row is None:
        return fingerprint, None
    if row["fingerprint"] != fingerprint:
        raise error(409, "conflict", "This key already belongs to a different request.")
    return fingerprint, shape(json.loads(row["result"]))
