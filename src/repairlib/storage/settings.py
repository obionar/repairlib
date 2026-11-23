"""Shop-owned settings and atomic, replayable configuration changes."""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from repairlib.contracts import ShopSettings
from repairlib.storage.dry_run import transaction
from repairlib.storage import request_ledger


def find(db, shop_id: int) -> ShopSettings:
    row = db.execute(
        """
        SELECT s.name, c.timezone, c.revision, c.max_upload_mib, c.inline_preview_mib,
               c.language, c.address, c.phone, c.terms FROM shops AS s
        JOIN shop_settings AS c ON c.shop_id = s.id WHERE s.id = ?
        """, (shop_id,),
    ).fetchone()
    return ShopSettings(*row)


def workspace_language(path: Path) -> str:
    """Stored default language of the workspace shop; empty when unset or unreadable."""
    try:
        with transaction(path, write=False) as db:
            row = db.execute(
                "SELECT c.language FROM local_workspace AS w "
                "JOIN shop_settings AS c ON c.shop_id = w.shop_id WHERE w.id = 1").fetchone()
    except sqlite3.Error:
        return ""
    return row[0] if row else ""


def update(db, shop_id: int, revision: int, name: str, timezone: str, max_upload_mib: int,
           inline_preview_mib: int, language: str, address: str = "", phone: str = "",
           terms: str = "") -> ShopSettings | None:
    row = db.execute(
        """
        UPDATE shop_settings SET timezone = ?, max_upload_mib = ?, inline_preview_mib = ?,
            language = ?, address = ?, phone = ?, terms = ?, revision = revision + 1
        WHERE shop_id = ? AND revision = ? RETURNING revision
        """, (timezone, max_upload_mib, inline_preview_mib, language, address, phone, terms,
               shop_id, revision),
    ).fetchone()
    if row is None:
        return None
    db.execute("UPDATE shops SET name = ? WHERE id = ?", (name, shop_id))
    return ShopSettings(name, timezone, row[0], max_upload_mib, inline_preview_mib, language,
                        address, phone, terms)


def diagnosis_fee(db, shop_id: int) -> int:
    return db.execute("SELECT diagnosis_fee_cents FROM shop_settings WHERE shop_id = ?", (shop_id,)).fetchone()[0]


def set_diagnosis_fee(db, shop_id: int, cents: int) -> None:
    db.execute("UPDATE shop_settings SET diagnosis_fee_cents = ? WHERE shop_id = ?", (cents, shop_id))


def attachment_limits(db, shop_id: int) -> tuple[int, int]:
    """Upload and inline-preview limits in bytes."""
    upload, inline = db.execute("SELECT max_upload_mib, inline_preview_mib FROM shop_settings "
                                "WHERE shop_id = ?", (shop_id,)).fetchone()
    return upload * 1024 * 1024, inline * 1024 * 1024


def previous_request(db, shop_id: int, actor_id: int, key: str):
    return request_ledger.find(db, "settings", shop_id, actor_id, None, key)


@dataclass(frozen=True, kw_only=True)
class SettingsReceipt(ShopSettings):
    diagnosis_fee_cents: int


def save_result(db, shop_id: int, actor_id: int, key: str, fingerprint: str,
                result: ShopSettings, diagnosis_fee_cents: int) -> None:
    receipt = SettingsReceipt(**vars(result), diagnosis_fee_cents=diagnosis_fee_cents)
    request_ledger.store(db, "settings", shop_id, actor_id, None, key, fingerprint, receipt)


def audit(db, shop_id: int, actor_id: int, username: str, source: str,
          token_label: str | None, changes: dict) -> None:
    db.execute(
        """
        INSERT INTO audit_events
            (shop_id, actor_kind, actor_user_id, actor_username, operation, source,
             entity_type, entity_id, changes, token_label)
        VALUES (?, 'user', ?, ?, 'settings.update', ?, 'shop_settings', ?, ?, ?)
        """, (shop_id, actor_id, username, source, shop_id, json.dumps(changes), token_label),
    )
