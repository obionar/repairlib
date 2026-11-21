"""Repair attachments: authorized staged uploads, downloads, detach and reconciliation."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import unicodedata

from repairlib.config import DISK_RESERVE_BYTES
from repairlib.contracts import (
    Attachment, AttachmentCaption, AttachmentDetach, AttachmentLink, AttachmentPage, AttachmentPurge,
)
from repairlib.domain.customers import MAX_ID, authorized
from repairlib.domain.replay import replay as replay_receipt
from repairlib.domain.repairs import RepairAccess, RepairError, find_repair, find_unarchived_repair
from repairlib.lifecycle.operations import private_subdirectory
from repairlib.lifecycle.workspace import (
    ACTIVE, WorkspaceError, database_path, maintenance_lock, workspace_status,
)
from repairlib.paths import check_database_files
from repairlib.storage import attachments, blobs, repairs, settings

# Hard ceiling; each shop's bounded settings (default 256 MiB upload, 8 MiB inline
# preview) choose the effective limits below it.
MAX_UPLOAD_BYTES = 2048 * 1024 * 1024
# Whole-workspace archives and updates carry at most this many retained files (see lifecycle/backup_archive).
MAX_RETAINED_ATTACHMENTS = 10_000
PURGE_AFTER = timedelta(days=30)
MAX_FILENAME = 255
MAX_CAPTION = 1000
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff(head: bytes) -> str:
    """Classify by content signature only; client-declared types are never trusted."""
    for signature, media_type in SIGNATURES:
        if head.startswith(signature):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def normalize_filename(raw: str) -> str:
    name = unicodedata.normalize("NFC", raw.replace("\\", "/").rsplit("/", 1)[-1].strip())
    if not name or name in {".", ".."}:
        raise RepairError(422, "validation", "Check the attachment.", {"filename": "Enter a file name."})
    if len(name) > MAX_FILENAME or any(not char.isprintable() for char in name):
        raise RepairError(422, "validation", "Check the attachment.",
                          {"filename": f"Use at most {MAX_FILENAME} printable characters."})
    return name


def check_entry(db, shop_id: int, repair_id: int, entry_id: int | None, *,
                visible_only: bool = True) -> None:
    if entry_id is not None and not (
        1 <= entry_id <= MAX_ID
        and attachments.entry_exists(db, shop_id, repair_id, entry_id, visible_only=visible_only)
    ):
        message = ("Choose a visible work entry from this repair." if visible_only
                   else "Choose a work entry from this repair.")
        raise RepairError(422, "validation", "Check the attachment.", {"entry_id": message})


def check_upload_space(workspace: Path, size: int) -> None:
    if shutil.disk_usage(database_path(workspace).parent).free < DISK_RESERVE_BYTES + size:
        raise RepairError(507, "insufficient_storage", "Not enough disk space for this attachment. Free up space and retry.")


def prepare_upload(workspace: Path, access: RepairAccess, repair_id: int,
                   filename: str, entry_id: int | None, *, declared_size: int = 0) -> blobs.StagedBlob:
    """Refuse cheaply before accepting any bytes, then open a private staging file."""
    normalize_filename(filename)
    with authorized(workspace, access) as (db, principal, _, can_write):
        if not can_write:
            raise RepairError(403, "forbidden", "Your account or token cannot add attachments.")
        find_unarchived_repair(db, principal.shop_id, repair_id)
        check_entry(db, principal.shop_id, repair_id, entry_id)
        upload_limit, _ = settings.attachment_limits(db, principal.shop_id)
    check_upload_space(workspace, declared_size)
    return blobs.StagedBlob(workspace, min(MAX_UPLOAD_BYTES, upload_limit))


def too_large(limit: int | None = None) -> RepairError:
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    size = f"{limit // (1024 * 1024)} MiB" if limit >= 1024 * 1024 else f"{limit} bytes"
    return RepairError(413, "too_large", f"Files may be at most {size}.")


def check_declared_length(length: str | None, limit: int | None = None) -> int:
    """Refuse an oversized declared body before reading it; streaming still enforces the limit."""
    if length is None:
        return 0
    if not length.isascii() or not length.isdecimal():
        raise RepairError(400, "request_body", "Invalid request length.")
    digits = length.lstrip("0")
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    if len(digits) > 19 or int(digits or "0") > limit:
        raise too_large(limit)
    return int(digits or "0")


def write_chunk(upload: blobs.StagedBlob, chunk: bytes) -> None:
    if upload.size + len(chunk) <= upload.limit:
        check_upload_space(upload.workspace, len(chunk))
    try:
        upload.write(chunk)
    except blobs.BlobTooLarge:
        raise too_large(upload.limit) from None


def commit_upload(workspace: Path, access: RepairAccess, repair_id: int, upload: blobs.StagedBlob,
                  *, filename: str, entry_id: int | None, key: str) -> Attachment:
    """Publish original bytes before committing their metadata."""
    try:
        upload.finish()
        name = normalize_filename(filename)
        if upload.size == 0:
            raise RepairError(422, "validation", "Choose a non-empty file.", {"file": "The file is empty."})
        operation = "attachment.create"
        with authorized(workspace, access, write=True) as (db, principal, token_label, _):
            find_unarchived_repair(db, principal.shop_id, repair_id)
            payload = {"repair_id": repair_id, "entry_id": entry_id, "filename": name,
                       "size_bytes": upload.size, "sha256": upload.sha256}
            fingerprint, previous = replay_attachment(db, principal, operation, key, payload)
            if previous is not None:
                return previous
            check_entry(db, principal.shop_id, repair_id, entry_id)
            if attachments.retained_count(db) >= MAX_RETAINED_ATTACHMENTS:
                raise RepairError(409, "attachment_limit",
                                  "This workspace holds the most files a backup can carry. Purge detached files first.")
            storage_name = upload.publish(principal.shop_id)
            _, inline_limit = settings.attachment_limits(db, principal.shop_id)
            created = attachments.insert(db, principal, repair_id, entry_id, name, sniff(upload.head),
                                         upload.size, upload.sha256, storage_name, inline_limit)
            attachments.save_result(db, principal.shop_id, principal.user_id, operation, key,
                                    fingerprint, created)
            audit(db, principal, access.source, token_label, operation, created.id,
                  {"repair_id": repair_id, "entry_id": entry_id, "filename": name,
                   "size_bytes": upload.size, "sha256": upload.sha256})
            # From this point the transaction may commit. If its outcome is
            # uncertain, keep the original; reconciliation can remove orphans.
            upload.keep()
        return created
    finally:
        upload.discard()


def attachment_shape(result: dict) -> Attachment:
    legacy_entry = result["entry_id"]
    result.setdefault("entry_ids", [] if legacy_entry is None else [legacy_entry])
    return Attachment(**result)


def replay_attachment(db, principal, operation: str, key: str, payload: dict):
    return replay_receipt(db, principal, "attachment", operation, key, payload, attachment_shape, RepairError)


def audit(db, principal, source, token_label, operation, attachment_id, changes):
    repairs.audit(db, principal, source, token_label, operation, "attachment", attachment_id, changes)


def list_attachments(workspace: Path, access: RepairAccess, repair_id: int, *,
                     before: int | None = None, limit: int = 25, detached: bool | None = None):
    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
        raise RepairError(422, "validation", "Invalid attachment page.")
    with authorized(workspace, access) as (db, principal, _, can_write):
        find_repair(db, principal.shop_id, repair_id)
        _, inline_limit = settings.attachment_limits(db, principal.shop_id)
        rows = attachments.list_page(db, principal.shop_id, repair_id, before, limit + 1,
                                     inline_limit, detached)
    page = rows[:limit]
    return AttachmentPage(page, page[-1].id if len(rows) > limit else None), can_write


def upload_context(workspace: Path, access: RepairAccess) -> tuple[int, bool]:
    """Effective upload limit in MiB and whether the caller may purge, for form hints."""
    with authorized(workspace, access) as (db, principal, _, _):
        upload_limit, _ = settings.attachment_limits(db, principal.shop_id)
        return max(min(MAX_UPLOAD_BYTES, upload_limit) // (1024 * 1024), 1), principal.role == "admin"


def find_attachment(db, shop_id: int, repair_id: int, attachment_id: int):
    find_repair(db, shop_id, repair_id)
    _, inline_limit = settings.attachment_limits(db, shop_id)
    found, storage_name = (attachments.find(db, shop_id, repair_id, attachment_id, inline_limit)
                           if 1 <= attachment_id <= MAX_ID else (None, None))
    if found is None:
        raise RepairError(404, "not_found", "Attachment not found.")
    return found, storage_name


def read_attachment(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int) -> Attachment:
    with authorized(workspace, access) as (db, principal, _, _):
        return find_attachment(db, principal.shop_id, repair_id, attachment_id)[0]


def attachment_content(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int,
                       *, inline: bool) -> tuple[Attachment, Path]:
    with authorized(workspace, access) as (db, principal, _, _):
        found, storage_name = find_attachment(db, principal.shop_id, repair_id, attachment_id)
        shop_id = principal.shop_id
    if found.purged:
        raise RepairError(410, "purged", "The attachment bytes were purged; its record and history remain.")
    if inline and not found.previewable:
        raise RepairError(415, "not_previewable", "Only small PNG, JPEG, GIF or WebP images preview inline.")
    path = blobs.blob_path(workspace, shop_id, storage_name)
    if not path.is_file() or path.is_symlink():
        raise RepairError(503, "file_unavailable",
                          "The attachment file is unavailable. Ask the owner to run file reconciliation.")
    return found, path


def review_metadata(db, principal, repair_id: int, attachment_id: int, operation: str, key: str,
                    change: AttachmentCaption | AttachmentLink):
    payload = {"attachment_id": attachment_id, **asdict(change)}
    fingerprint, previous = replay_attachment(db, principal, operation, key, payload)
    if previous is not None:
        return None, fingerprint, previous
    find_unarchived_repair(db, principal.shop_id, repair_id)
    current, _ = find_attachment(db, principal.shop_id, repair_id, attachment_id)
    if current.purged:
        raise RepairError(409, "purged", "Purged attachments cannot be edited.")
    if current.detached:
        raise RepairError(409, "detached", "Restore the attachment before editing it.")
    if current.revision != change.revision:
        raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
    return current, fingerprint, None


def edit_caption(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int,
                 change: AttachmentCaption, *, key: str) -> Attachment:
    if not 1 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed attachment revision.")
    caption = change.caption.strip()
    if len(caption) > MAX_CAPTION or any(not char.isprintable() for char in caption):
        raise RepairError(422, "validation", "Check the attachment caption.",
                          {"caption": "Use at most 1000 printable characters."})
    operation = "attachment.caption"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        current, fingerprint, previous = review_metadata(
            db, principal, repair_id, attachment_id, operation, key, AttachmentCaption(change.revision, caption))
        if previous is not None:
            return previous
        _, inline_limit = settings.attachment_limits(db, principal.shop_id)
        updated = attachments.set_caption(db, principal.shop_id, current, caption, inline_limit)
        if updated is None:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        attachments.save_result(db, principal.shop_id, principal.user_id, operation, key,
                                fingerprint, updated)
        audit(db, principal, access.source, token_label, operation, attachment_id,
              {"repair_id": repair_id, "caption": {"before": current.caption, "after": caption}})
        return updated


def change_entry_link(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int,
                      change: AttachmentLink, *, key: str) -> Attachment:
    if not 1 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed attachment revision.")
    operation = "attachment.link"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        current, fingerprint, previous = review_metadata(
            db, principal, repair_id, attachment_id, operation, key, change)
        if previous is not None:
            return previous
        check_entry(db, principal.shop_id, repair_id, change.entry_id, visible_only=change.linked)
        if (change.entry_id in current.entry_ids) == change.linked:
            raise RepairError(409, "same_state", "The attachment already has this link state.")
        _, inline_limit = settings.attachment_limits(db, principal.shop_id)
        updated = attachments.set_entry_link(db, principal.shop_id, current, change.entry_id,
                                             change.linked, inline_limit)
        if updated is None:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        attachments.save_result(db, principal.shop_id, principal.user_id, operation, key,
                                fingerprint, updated)
        audit(db, principal, access.source, token_label,
              "attachment.link" if change.linked else "attachment.unlink", attachment_id,
              {"repair_id": repair_id, "entry_id": change.entry_id,
               "linked": {"before": not change.linked, "after": change.linked}})
        return updated


def change_detached(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int,
                    change: AttachmentDetach, *, key: str) -> Attachment:
    if not 1 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed attachment revision.")
    operation = "attachment.detach"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"attachment_id": attachment_id, "revision": change.revision,
                   "detached": change.detached}
        fingerprint, previous = replay_attachment(db, principal, operation, key, payload)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        current, _ = find_attachment(db, principal.shop_id, repair_id, attachment_id)
        if current.purged:
            raise RepairError(409, "purged", "Purged attachments cannot be restored.")
        if current.revision != change.revision:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        if current.detached == change.detached:
            raise RepairError(409, "same_state", "The attachment already has this state.")
        timestamp = (datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                     if change.detached else None)
        _, inline_limit = settings.attachment_limits(db, principal.shop_id)
        updated = attachments.set_detached(db, principal.shop_id, current, timestamp, inline_limit)
        if updated is None:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        attachments.save_result(db, principal.shop_id, principal.user_id, operation, key,
                                fingerprint, updated)
        audit(db, principal, access.source, token_label,
              "attachment.detach" if change.detached else "attachment.restore", attachment_id,
              {"repair_id": repair_id, "detached": {"before": current.detached, "after": change.detached}})
        return updated


def utc_text(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def purge_attachment(workspace: Path, access: RepairAccess, repair_id: int, attachment_id: int,
                     request: AttachmentPurge, *, key: str) -> Attachment:
    """Admin retention: delete bytes detached for 30+ days; keep metadata, hash and history.

    The record commits first; bytes are removed afterwards. A crash in between leaves an
    unreferenced file that `reconcile-files` removes, never a record pointing at nothing.
    """
    if not 1 <= request.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed attachment revision.")
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        if principal.role != "admin":
            raise RepairError(403, "forbidden", "Only administrators can purge attachment bytes.")
        current, storage_name = find_attachment(db, principal.shop_id, repair_id, attachment_id)
        payload = {"attachment_id": attachment_id, "revision": request.revision, "purge": True}
        # Purge retries share the detach request namespace; the payload keeps them distinct.
        fingerprint, previous = replay_attachment(db, principal, "attachment.detach", key, payload)
        if previous is not None:
            return previous
        if current.purged:
            raise RepairError(409, "same_state", "The attachment bytes were already purged.")
        if current.revision != request.revision:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        now = datetime.now(UTC)
        if not current.detached or current.detached_at > utc_text(now - PURGE_AFTER):
            raise RepairError(409, "retention",
                              "Only attachments detached for at least 30 days can be purged.")
        purged = attachments.purge(db, principal, current, utc_text(now), utc_text(now - PURGE_AFTER))
        if purged is None:
            raise RepairError(409, "stale_revision", "The attachment changed. Review the latest version.")
        attachments.save_result(db, principal.shop_id, principal.user_id, "attachment.detach", key,
                                fingerprint, purged)
        audit(db, principal, access.source, token_label, "attachment.purge", attachment_id,
              {"repair_id": repair_id, "sha256": current.sha256, "size_bytes": current.size_bytes,
               "detached_at": current.detached_at})
        shop_id = principal.shop_id
    blobs.remove_blob(workspace, shop_id, storage_name)
    return purged


def reconcile_files(workspace: Path) -> dict:
    """Stopped-app maintenance: remove never-acknowledged files and report damaged records.

    Uploads publish bytes before their metadata transaction commits, so an interrupted
    upload can leave a staging file or an unreferenced published file. Neither was ever
    acknowledged to a client. Recorded attachments are never deleted; missing or
    mismatched bytes are reported for recovery from backup.
    """
    with maintenance_lock(workspace, exclusive=True):
        if workspace_status(workspace) != "initialized":
            raise WorkspaceError("Workspace must be initialized and use the current schema.")
        path = database_path(workspace)
        check_database_files(path)
        recorded = attachments.all_stored(path)
        known: dict[str, list[tuple[int, str, str]]] = {}
        for shop_id, attachment_id, name, digest in recorded:
            directory = blobs.blob_path(workspace, shop_id, name).parent.name
            known.setdefault(directory, []).append((attachment_id, name, digest))
        missing, mismatched = [], []
        staging = orphans = 0
        with private_subdirectory(workspace, ACTIVE, blobs.FILES) as files:
            if files is None:
                missing = [attachment_id for _, attachment_id, _, _ in recorded]
            else:
                with private_subdirectory(files, blobs.STAGING) as stages:
                    if stages is not None:
                        staging = blobs.remove_unreferenced(stages, blobs.UPLOAD_PART)
                for directory in sorted(known.keys() | set(os.listdir(files))):
                    if not re.fullmatch(r"shop-[1-9][0-9]*", directory):
                        continue
                    rows = known.get(directory, ())
                    with private_subdirectory(files, directory) as shop:
                        if shop is None:
                            missing.extend(attachment_id for attachment_id, _, _ in rows)
                            continue
                        for attachment_id, name, digest in rows:
                            actual = blobs.file_digest(shop, name)
                            if actual is None:
                                missing.append(attachment_id)
                            elif actual != digest:
                                mismatched.append(attachment_id)
                        orphans += blobs.remove_unreferenced(shop, blobs.STORAGE_NAME, {name for _, name, _ in rows})
        report = {"checked": len(recorded), "removed_staging": staging,
                  "removed_orphans": orphans, "missing": sorted(missing), "mismatched": sorted(mismatched)}
        attachments.audit_reconciliation(path, report)
        return report
