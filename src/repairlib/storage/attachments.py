"""Shop-scoped attachment metadata, retry results and reconciliation queries."""

import json
from pathlib import Path

from repairlib.contracts import Attachment
from repairlib.storage.customers import transaction
from repairlib.storage import request_ledger

INLINE_LIMIT = 8 * 1024 * 1024
COLUMNS = ("id, repair_id, filename, media_type, size_bytes, sha256, uploader_id, "
           "uploader_username, created_at, revision, detached_at IS NOT NULL AS detached, detached_at, "
           "purged_at IS NOT NULL AS purged, purged_at, purged_by_username, caption, role")
LINKS = """(SELECT json_group_array(entry_id) FROM (
    SELECT entry_id FROM attachment_links
    WHERE shop_id = attachments.shop_id AND repair_id = attachments.repair_id
        AND attachment_id = attachments.id ORDER BY entry_id
)) AS entry_ids"""
DETAIL_COLUMNS = f"{COLUMNS}, {LINKS}"


def record(row, inline_limit: int = INLINE_LIMIT) -> Attachment:
    values = dict(row)
    values["entry_ids"] = json.loads(values["entry_ids"])
    values["entry_id"] = next(iter(values["entry_ids"]), None)
    values["detached"] = bool(values["detached"])
    values["purged"] = bool(values["purged"])
    values["previewable"] = (values["media_type"].startswith("image/") and not values["purged"]
                             and values["size_bytes"] <= inline_limit)
    return Attachment(**values)


def retained_count(db) -> int:
    """Files whose bytes are kept; a whole-workspace backup carries exactly these."""
    return db.execute("SELECT COUNT(*) FROM attachments WHERE purged_at IS NULL").fetchone()[0]


def entry_exists(db, shop_id: int, repair_id: int, entry_id: int, *, visible_only: bool) -> bool:
    return db.execute(
        """SELECT 1 FROM work_entries
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND (NOT ? OR deleted_at IS NULL)""",
        (shop_id, repair_id, entry_id, visible_only),
    ).fetchone() is not None


def insert(db, principal, repair_id: int, entry_id: int | None, filename: str, media_type: str,
           size_bytes: int, digest: str, storage_name: str, inline_limit: int, role: str = "") -> Attachment:
    folder, slash, name = storage_name.rpartition("/")
    attachment_id = db.execute(
        """INSERT INTO attachments(shop_id, repair_id, uploader_id, uploader_username,
                                   filename, media_type, size_bytes, sha256, storage_folder, storage_name, role)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (principal.shop_id, repair_id, principal.user_id, principal.username,
         filename, media_type, size_bytes, digest, folder + slash, name, role),
    ).fetchone()["id"]
    if entry_id is not None:
        insert_link(db, principal.shop_id, repair_id, attachment_id, entry_id)
    return find(db, principal.shop_id, repair_id, attachment_id, inline_limit)[0]


def insert_link(db, shop_id: int, repair_id: int, attachment_id: int, entry_id: int) -> None:
    db.execute(
        """INSERT INTO attachment_links (shop_id, repair_id, attachment_id, entry_id)
        VALUES (?, ?, ?, ?)""", (shop_id, repair_id, attachment_id, entry_id),
    )


def set_entry_link(db, shop_id: int, current: Attachment, entry_id: int, linked: bool,
                   inline_limit: int) -> Attachment | None:
    changed = db.execute(
        """UPDATE attachments SET revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ?""",
        (shop_id, current.repair_id, current.id, current.revision),
    )
    if not changed.rowcount:
        return None
    if linked:
        insert_link(db, shop_id, current.repair_id, current.id, entry_id)
    else:
        db.execute(
            """DELETE FROM attachment_links
            WHERE shop_id = ? AND repair_id = ? AND attachment_id = ? AND entry_id = ?""",
            (shop_id, current.repair_id, current.id, entry_id),
        )
    return find(db, shop_id, current.repair_id, current.id, inline_limit)[0]


def find(db, shop_id: int, repair_id: int, attachment_id: int, inline_limit: int):
    row = db.execute(
        f"SELECT {DETAIL_COLUMNS}, storage_folder || storage_name AS storage_name FROM attachments "
        "WHERE shop_id = ? AND repair_id = ? AND id = ?",
        (shop_id, repair_id, attachment_id),
    ).fetchone()
    if row is None:
        return None, None
    values = dict(row)
    storage_name = values.pop("storage_name")
    return record(values, inline_limit), storage_name


def list_page(db, shop_id: int, repair_id: int, before: int | None, limit: int,
              inline_limit: int, detached: bool | None = None) -> list[Attachment]:
    detached = None if detached is None else int(detached)
    rows = db.execute(
        f"""SELECT {DETAIL_COLUMNS} FROM attachments
        WHERE shop_id = ? AND repair_id = ? AND (? IS NULL OR id < ?)
          AND (? IS NULL OR ((detached_at IS NOT NULL) = ?))
        ORDER BY id DESC LIMIT ?""",
        (shop_id, repair_id, before, before, detached, detached, limit),
    )
    return [record(row, inline_limit) for row in rows]


def set_caption(db, shop_id: int, current: Attachment, caption: str,
                inline_limit: int) -> Attachment | None:
    row = db.execute(
        f"""UPDATE attachments SET caption = ?, revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ?
        RETURNING {DETAIL_COLUMNS}""",
        (caption, shop_id, current.repair_id, current.id, current.revision),
    ).fetchone()
    return record(row, inline_limit) if row is not None else None


def set_detached(db, shop_id: int, current: Attachment, timestamp: str | None,
                 inline_limit: int) -> Attachment | None:
    row = db.execute(
        f"""UPDATE attachments SET detached_at = ?, revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ? AND purged_at IS NULL
        RETURNING {DETAIL_COLUMNS}""",
        (timestamp, shop_id, current.repair_id, current.id, current.revision),
    ).fetchone()
    return record(row, inline_limit) if row is not None else None


def purge(db, principal, current: Attachment, timestamp: str, cutoff: str) -> Attachment | None:
    row = db.execute(
        f"""UPDATE attachments SET purged_at = ?, purged_by_username = ?, revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ? AND purged_at IS NULL
            AND detached_at IS NOT NULL AND detached_at <= ?
        RETURNING {DETAIL_COLUMNS}""",
        (timestamp, principal.username, principal.shop_id, current.repair_id, current.id,
         current.revision, cutoff),
    ).fetchone()
    return record(row) if row is not None else None


def save_result(db, shop_id: int, actor_id: int, operation: str, key: str,
                fingerprint: str, result: Attachment) -> None:
    request_ledger.store(db, "attachment", shop_id, actor_id, operation, key, fingerprint, result,
                         link=result.id)


def ticket_pictures(db, shop_id: int, repair_id: int) -> tuple[int | None, dict[int, int]]:
    """Newest live device photo, and the first live image linked to each entry."""
    live = "a.detached_at IS NULL AND a.purged_at IS NULL AND a.media_type <> 'application/octet-stream'"
    photo = db.execute(f"""SELECT a.id FROM attachments AS a
        WHERE a.shop_id = ? AND a.repair_id = ? AND a.role = 'device' AND {live}
        ORDER BY a.id DESC LIMIT 1""", (shop_id, repair_id)).fetchone()
    pictures = db.execute(f"""SELECT l.entry_id, MIN(a.id) FROM attachment_links AS l
        JOIN attachments AS a ON a.shop_id = l.shop_id AND a.repair_id = l.repair_id AND a.id = l.attachment_id
        WHERE l.shop_id = ? AND l.repair_id = ? AND {live} GROUP BY l.entry_id""", (shop_id, repair_id))
    return (photo[0] if photo else None), {entry: attachment for entry, attachment in pictures}


def all_stored(path: Path) -> list[tuple[int, int, str, str]]:
    with transaction(path, write=False) as db:
        return [tuple(row) for row in db.execute(
            "SELECT shop_id, id, storage_folder || storage_name, sha256 FROM attachments WHERE purged_at IS NULL "
            "ORDER BY shop_id, id")]


def audit_reconciliation(path: Path, changes: dict) -> None:
    with transaction(path, write=True) as db:
        shop_id = db.execute("SELECT shop_id FROM local_workspace WHERE id = 1").fetchone()[0]
        db.execute(
            """INSERT INTO audit_events(shop_id, actor_kind, operation, source, changes)
            VALUES (?, 'maintenance', 'files.reconcile', 'cli', ?)""",
            (shop_id, json.dumps(changes)),
        )
