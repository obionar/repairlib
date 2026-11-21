"""Shop-owned CSV review slots and committed batch receipts."""


from repairlib.contracts import CsvApply
from repairlib.storage import request_ledger


def stage(db, principal, kind, preview_id, input_sha256, context_sha256) -> None:
    db.execute("""
        INSERT INTO csv_previews (shop_id, actor_id, kind, preview_id, input_sha256, context_sha256)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (shop_id, actor_id, kind) DO UPDATE SET
            preview_id = excluded.preview_id, input_sha256 = excluded.input_sha256,
            context_sha256 = excluded.context_sha256
        """, (principal.shop_id, principal.user_id, kind, preview_id, input_sha256, context_sha256))


def review(db, principal, kind, preview_id):
    return db.execute("""
        SELECT input_sha256, context_sha256 FROM csv_previews
        WHERE shop_id = ? AND actor_id = ? AND kind = ? AND preview_id = ?
        """, (principal.shop_id, principal.user_id, kind, preview_id)).fetchone()


def clear_review(db, principal, kind) -> None:
    db.execute("DELETE FROM csv_previews WHERE shop_id = ? AND actor_id = ? AND kind = ?",
               (principal.shop_id, principal.user_id, kind))


def previous_request(db, principal, kind, key):
    return request_ledger.find(db, "csv", principal.shop_id, principal.user_id, kind, key)


def save_result(db, principal, kind, key, fingerprint, result: CsvApply) -> None:
    request_ledger.store(db, "csv", principal.shop_id, principal.user_id, kind, key, fingerprint, result)
