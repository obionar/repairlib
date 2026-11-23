"""Authenticated portable shop export, preview and import."""

from contextlib import contextmanager
from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from struct import Struct, error as StructError
from tempfile import TemporaryFile
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

from repairlib import money
from repairlib.config import DISK_RESERVE_BYTES
from repairlib.contracts import PORTABLE_MAX_BYTES, PortableImport, PortablePreview, PortableSourceShop, TokenIdentity
from repairlib.domain.attachments import MAX_UPLOAD_BYTES, sniff
from repairlib.domain.customers import KEY_FORMAT, CustomerAccess, CustomerError, authorized
from repairlib.domain.settings import normalize_timezone
from repairlib.lifecycle.backup_archive import (
    BoundedZipReader, CHUNK, MANIFEST, SHA256, archive_entry, check_local_entries,
    hash_stream, private_file, regular_stored_entry, unique_json_object,
)
from repairlib.lifecycle.operations import private_subdirectory
from repairlib.lifecycle.workspace import WorkspaceError
from repairlib.paths import database_path
from repairlib.storage import audit, blobs, portable_shop as storage
from repairlib.storage.blobs import blob_path
from repairlib.storage.stock_reconciliation import repair_snapshot_issues
from repairlib.storage.portable_shop import (
    AUDIT_TARGETS, COLUMNS, MAINTENANCE_HISTORY, MAX_ID, MAX_RECORDS, SCHEMA, RecordError,
    retained_storage, rows, source_name, validate_records,
)

FORMAT = "repairlib-portable-shop-1"
RECORDS = "records.json"
MAX_RECORD_BYTES = 16 << 20
MAX_MANIFEST_BYTES = 2 << 20
MAX_ATTACHMENTS = 10_000  # includes metadata for purged files
ATTACHMENT_MEMBER = re.compile(r"attachments/[1-9][0-9]{0,18}")
END_DIRECTORY = Struct("<4s4H2IH")


class ArchiveError(ValueError):
    """The portable artifact cannot be built or does not meet the format contract."""


def require_admin(principal) -> int:
    if principal.role != "admin":
        raise CustomerError(403, "forbidden", "Only shop administrators can use portable archives.")
    return principal.shop_id


def authorize_preview(workspace: Path, access: CustomerAccess) -> int:
    with authorized(workspace, access, resource="portable archives") as (_, principal, _, _):
        return require_admin(principal)


def source_identity(value) -> PortableSourceShop:
    if (not isinstance(value, dict) or value.keys() != {"id", "name"}
            or type(value["id"]) is not int or not 1 <= value["id"] <= MAX_ID
            or not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 160
            or value["name"].strip() != value["name"] or not value["name"].isprintable()):
        raise ArchiveError("The source shop identity is invalid.")
    return PortableSourceShop(**value)


def invalid_constant(_value):
    raise ArchiveError("A portable archive JSON value is invalid.")


def parse_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_object,
                      parse_constant=invalid_constant)


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def encode_records(db, shop_id: int) -> bytes:
    output = BytesIO()
    count = 0

    def emit(value: bytes):
        if output.tell() + len(value) > MAX_RECORD_BYTES:
            raise ArchiveError("Portable records exceed their size limit.")
        output.write(value)

    emit(b"{")
    for index, table in enumerate(COLUMNS):
        emit((b"," if index else b"") + encode_json(table) + b":[")
        for offset, row in enumerate(rows(db, shop_id, table)):
            count += 1
            if count > MAX_RECORDS or (table == "attachments" and offset >= MAX_ATTACHMENTS):
                raise ArchiveError("Portable records exceed their count limit.")
            emit((b"," if offset else b"") + encode_json(dict(row)))
        emit(b"]")
    emit(b"}")
    return output.getvalue()


def record_counts(records) -> dict[str, int]:
    if not isinstance(records, dict) or records.keys() != COLUMNS.keys():
        raise ArchiveError("The portable records do not match the required tables.")
    if any(not isinstance(items, list) for items in records.values()):
        raise ArchiveError("The portable records must be lists of rows.")
    counts = {table: len(items) for table, items in records.items()}
    if (sum(counts.values()) > MAX_RECORDS or counts["attachments"] > MAX_ATTACHMENTS
            or counts["shop_settings"] != 1):
        raise ArchiveError("Portable record counts are invalid or exceed their limits.")
    return counts


def check_record_details(records) -> dict:
    settings = records["shop_settings"][0]
    try:
        if normalize_timezone(settings["timezone"]) != settings["timezone"]:
            raise ArchiveError("The portable timezone is invalid.")
    except CustomerError as error:
        raise ArchiveError("The portable timezone is invalid.") from error
    for row in records["audit_events"]:
        entity = row["entity_type"]
        if entity not in AUDIT_TARGETS and not (
            entity is None and row["entity_id"] is None and row["actor_kind"] == "maintenance"
            and row["operation"] in MAINTENANCE_HISTORY
        ):
            raise ArchiveError("The archive contains unsupported audit history.")
        if row["changes"] is not None and not isinstance(parse_json(row["changes"].encode()), dict):
            raise ArchiveError("An audit event has invalid changes.")

    files = {}
    for row in records["attachments"]:
        size, digest = row["size_bytes"], row["sha256"]
        purged = row["purged_at"] is not None
        if (not 0 < size <= MAX_UPLOAD_BYTES or not SHA256.fullmatch(digest)
                or purged != (row["purged_by_username"] is not None)
                or (purged and row["detached_at"] is None)):
            raise ArchiveError("An attachment has invalid portable metadata.")
        if not purged:
            files[f"attachments/{row['id']}"] = {"size": size, "sha256": digest}
    return files


def inspect_records(raw: bytes, source: PortableSourceShop):
    records = parse_json(raw)
    counts = record_counts(records)
    validate_records(records, source.id, source.name)
    files = check_record_details(records)
    preview = PortablePreview(FORMAT, SCHEMA, source, counts, len(files),
                              sum(record["size"] for record in files.values()))
    return preview, files, records


def write_export(workspace: Path, db, shop_id: int, handle: BinaryIO) -> None:
    source = source_identity({"id": shop_id, "name": source_name(db, shop_id)})
    raw = encode_records(db, shop_id)
    preview, files, _ = inspect_records(raw, source)
    manifest_files = {RECORDS: hash_stream(BytesIO(raw), len(raw)), **files}
    manifest = encode_json({
        "format": FORMAT, "schema": SCHEMA, "source_shop": {"id": source.id, "name": source.name},
        "counts": preview.counts, "files": manifest_files,
    })
    # Reserve the maximum central directory size on this writer; local headers are
    # predictable, with no extra fields, data descriptors, or ZIP64 entries.
    zip_overhead = END_DIRECTORY.size + sum(76 + 2 * len(name) for name in (*manifest_files, MANIFEST))
    if (len(raw) + preview.attachment_bytes + len(manifest) + zip_overhead > PORTABLE_MAX_BYTES
            or len(manifest) > MAX_MANIFEST_BYTES):
        raise ArchiveError("The portable archive exceeds its size limit.")
    with ZipFile(handle, "w") as archive:
        archive.writestr(archive_entry(RECORDS, len(raw)), raw)
        for attachment_id, storage_name in retained_storage(db, shop_id):
            member = f"attachments/{attachment_id}"
            record = files[member]
            path = blob_path(Path(), shop_id, storage_name)
            with private_subdirectory(workspace, *path.parts[:-1]) as directory:
                if directory is None:
                    raise ArchiveError("A retained attachment is unavailable.")
                with private_file(path.name, directory=directory) as original:
                    size = record["size"]
                    if os.fstat(original.fileno()).st_size != size:
                        raise ArchiveError("A retained attachment does not match its metadata.")
                    with archive.open(archive_entry(member, size), "w") as entry:
                        if hash_stream(original, size, entry) != record:
                            raise ArchiveError("A retained attachment does not match its metadata.")
        archive.writestr(archive_entry(MANIFEST, len(manifest)), manifest)


def export_archive(workspace: Path, access: CustomerAccess) -> tuple[BinaryIO, int]:
    """Stage the whole verified export before returning bytes to the adapter."""
    handle = None
    try:
        with authorized(workspace, access, resource="portable archives") as (db, principal, _, _):
            shop_id = require_admin(principal)
            if isinstance(principal, TokenIdentity) and principal.kind == "agent":
                raise CustomerError(403, "forbidden", "Agent tokens cannot export portable archives.")
            handle = TemporaryFile()
            # Metadata comes from one read snapshot. Concurrent purge can remove a
            # file before it is opened. In that case discard the entire export.
            write_export(workspace, db, shop_id, handle)
            size = handle.tell()
            handle.seek(0)
        return handle, size
    except (WorkspaceError, ValueError, OSError, RecursionError) as error:
        if handle is not None:
            handle.close()
        raise CustomerError(409, "export_unavailable",
                            "Could not create a verified portable shop archive.") from error
    except BaseException:
        if handle is not None:
            handle.close()
        raise


def archive_members(handle: BinaryIO, archive: ZipFile, size: int):
    handle.seek(size - END_DIRECTORY.size)
    magic, disk, start_disk, here, entries, dir_size, offset, comment = END_DIRECTORY.unpack(
        handle.read(END_DIRECTORY.size))
    infos = archive.infolist()
    if (magic != b"PK\x05\x06" or disk or start_disk or comment
            or here != entries or entries != len(infos) or entries > MAX_ATTACHMENTS + 2
            or offset != archive.start_dir or offset + dir_size != size - END_DIRECTORY.size):
        raise ArchiveError("The archive has an unsupported ZIP directory.")
    members = {}
    for entry in infos:
        name = entry.orig_filename
        if name == MANIFEST:
            limit = MAX_MANIFEST_BYTES
        elif name == RECORDS:
            limit = MAX_RECORD_BYTES
        elif ATTACHMENT_MEMBER.fullmatch(name):
            limit = PORTABLE_MAX_BYTES
        else:
            raise ArchiveError("The archive contains an unsupported member path.")
        if (entry.filename != name or name in members or not regular_stored_entry(entry)
                or entry.extra or entry.comment or not 0 < entry.file_size <= limit):
            raise ArchiveError("The archive contains a duplicate, invalid or unsupported member.")
        members[name] = entry
    if MANIFEST not in members or RECORDS not in members:
        raise ArchiveError("The archive is missing its manifest or records.")
    check_local_entries(handle, archive, members)
    return members


def check_manifest(manifest, members) -> PortableSourceShop:
    if (not isinstance(manifest, dict)
            or manifest.keys() != {"format", "schema", "source_shop", "counts", "files"}
            or manifest["format"] != FORMAT or type(manifest["schema"]) is not int
            or manifest["schema"] != SCHEMA):
        raise ArchiveError("The portable archive format or schema is unsupported.")
    files = manifest["files"]
    if not isinstance(files, dict) or files.keys() != members.keys() - {MANIFEST}:
        raise ArchiveError("The ZIP members do not match the manifest.")
    for name, record in files.items():
        if (not isinstance(record, dict) or record.keys() != {"size", "sha256"}
                or type(record["size"]) is not int or record["size"] != members[name].file_size
                or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"])):
            raise ArchiveError("A manifest member has invalid size or hash metadata.")
    return source_identity(manifest["source_shop"])


@contextmanager
def verified_archive(handle: BinaryIO):
    size = handle.seek(0, os.SEEK_END)
    if not END_DIRECTORY.size <= size <= PORTABLE_MAX_BYTES:
        raise ArchiveError("The portable archive size is invalid or exceeds its limit.")
    with ZipFile(BoundedZipReader(handle)) as archive:
        members = archive_members(handle, archive, size)
        manifest = parse_json(archive.read(members[MANIFEST]))
        source = check_manifest(manifest, members)
        raw = archive.read(members[RECORDS])
        if hash_stream(BytesIO(raw), len(raw)) != manifest["files"][RECORDS]:
            raise ArchiveError("The records hash does not match the manifest.")
        preview, attachments, records = inspect_records(raw, source)
        declared_counts = manifest["counts"]
        if (not isinstance(declared_counts, dict)
                or any(type(count) is not int for count in declared_counts.values())
                or declared_counts != preview.counts):
            raise ArchiveError("The record counts do not match the manifest.")
        if attachments != {name: file for name, file in manifest["files"].items() if name != RECORDS}:
            raise ArchiveError("The retained attachments do not match the records.")
        for name, record in attachments.items():
            with archive.open(members[name]) as original:
                if hash_stream(original, record["size"]) != record:
                    raise ArchiveError("An attachment hash does not match the manifest.")
        yield preview, records, archive


def preview_archive(workspace: Path, access: CustomerAccess, handle: BinaryIO,
                    *, reviewed_shop: int) -> PortablePreview:
    # HTTP checked authority before staging the body. Recheck it after transport
    # with the authenticated destination context, independently of archive data.
    if authorize_preview(workspace, access) != reviewed_shop:
        raise CustomerError(403, "forbidden", "Your shop changed while staging the archive.")
    try:
        with verified_archive(handle) as (preview, _, _):
            return preview
    except (ArchiveError, RecordError) as error:
        raise CustomerError(422, "invalid_archive", str(error)) from None
    except (WorkspaceError, BadZipFile, ValueError, RecursionError, NotImplementedError,
            StructError, EOFError, OverflowError) as error:
        raise CustomerError(422, "invalid_archive",
                            "The portable archive contains invalid ZIP or JSON data.") from error


def confirm_destination(principal, destination_shop_id: int) -> int:
    shop_id = require_admin(principal)
    if destination_shop_id != shop_id:
        raise CustomerError(403, "forbidden", "The confirmed destination does not match your shop.")
    return shop_id


def authorize_import(workspace: Path, access: CustomerAccess, destination_shop_id: int) -> None:
    with authorized(workspace, access, resource="portable archives") as (_, principal, _, can_write):
        confirm_destination(principal, destination_shop_id)
        if not can_write:
            raise CustomerError(403, "forbidden", "A write token is required for portable import.")


def check_import_billing(records) -> None:
    subtotals = {}
    for charge in records["repair_charges"]:
        if charge["deleted_at"] is None:
            repair_id = charge["repair_id"]
            subtotal = subtotals.get(repair_id, 0) + money.line_total(
                charge["quantity_milli"], charge["unit_price_cents"])
            if subtotal > money.MAX_TOTAL:
                raise RecordError("An imported repair total exceeds its range.")
            subtotals[repair_id] = subtotal
    try:
        for row in records["repair_discounts"]:
            money.discount(row["kind"], row["value"], subtotals.get(row["repair_id"], 0))
        groups = {}
        for line in records["estimate_lines"]:
            total = money.line_total(line["quantity_milli"], line["unit_price_cents"])
            if total != line["line_total_cents"]:
                raise RecordError("An imported estimate line has an inconsistent total.")
            key = (line["estimate_id"], line["kind"])
            groups[key] = groups.get(key, 0) + total
        for row in records["estimates"]:
            if (groups.get((row["id"], "part"), 0) != row["parts_subtotal_cents"]
                    or groups.get((row["id"], "labor"), 0) != row["labor_subtotal_cents"]
                    or money.discount(row["discount_kind"], row["discount_value"], row["subtotal_cents"])
                    != row["discount_cents"]):
                raise RecordError("An imported estimate has inconsistent totals.")
    except ValueError as error:
        raise RecordError("An imported discount or estimate is inconsistent.") from error


def stage_originals(workspace, archive, records, attachment_ids, staged):
    for row in records["attachments"]:
        source_id = row["id"]
        destination_id = attachment_ids[source_id]
        if row["purged_at"] is not None:
            continue
        upload = blobs.StagedBlob(workspace, row["size_bytes"])
        staged[destination_id] = upload
        with archive.open(f"attachments/{source_id}") as original:
            while chunk := original.read(CHUNK):
                upload.write(chunk)
        upload.finish()
        if (upload.size != row["size_bytes"] or upload.sha256 != row["sha256"]
                or sniff(upload.head) != row["media_type"]):
            raise ArchiveError("Staged attachment bytes do not match their metadata.")


def import_archive(workspace: Path, access: CustomerAccess, handle: BinaryIO, *,
                   destination_shop_id: int, revision: int, key: str) -> PortableImport:
    authorize_import(workspace, access, destination_shop_id)
    if not KEY_FORMAT.fullmatch(key) or not 1 <= revision < MAX_ID:
        raise CustomerError(422, "validation", "Supply a valid key and reviewed destination revision.")
    staged = {}
    committed = False
    try:
        size = handle.seek(0, os.SEEK_END)
        if not END_DIRECTORY.size <= size <= PORTABLE_MAX_BYTES:
            raise ArchiveError("The portable archive size is invalid or exceeds its limit.")
        handle.seek(0)
        fingerprint = f"{revision}:{hash_stream(handle, size)['sha256']}"
        with verified_archive(handle) as (preview, records, archive):
            check_import_billing(records)
            with authorized(workspace, access, write=True,
                            resource="portable archives") as (db, principal, label, _):
                shop_id = confirm_destination(principal, destination_shop_id)
                previous = storage.import_receipt(db, shop_id)
                if previous is not None and previous["actor_id"] == principal.user_id and previous["request_key"] == key:
                    if previous["fingerprint"] != fingerprint:
                        raise CustomerError(409, "conflict", "This key already belongs to a different request.")
                    values = json.loads(previous["result"])
                    return PortableImport(**(values | {"source_shop": PortableSourceShop(**values["source_shop"])}))
                if previous is not None or not storage.is_empty(db, shop_id):
                    raise CustomerError(409, "destination_occupied", "The destination shop is occupied.")
                if storage.settings_revision(db, shop_id) != revision:
                    raise CustomerError(409, "stale_revision", "The destination settings changed. Review them again.")
                # shortcut: ZIP size estimates destination growth; budget SQLite separately if this proves insufficient.
                if shutil.disk_usage(database_path(workspace).parent).free < DISK_RESERVE_BYTES + size:
                    raise CustomerError(507, "insufficient_storage",
                                        "Not enough disk space for this import. Free up space and retry.")
                mapped, ids = storage.map_records(db, records, shop_id)
                names = {row["id"]: blobs.image_folder(row["media_type"], row["repair_id"])
                         + secrets.token_hex(blobs.STORAGE_NAME_LENGTH // 2)
                         for row in mapped["attachments"]}
                result = PortableImport(shop_id, revision + 1, preview.source_shop,
                                        preview.counts, preview.attachment_files)
                storage.insert_import(db, mapped, shop_id, names, revision, actor_id=principal.user_id,
                                      key=key, fingerprint=fingerprint, result=asdict(result))
                if repair_snapshot_issues(db, (shop_id, shop_id), portable=True):
                    raise RecordError("The stock ledger lacks matching repair audit history.")
                audit.record(db, principal, access.source, label, "portable.import", "shop_settings",
                              shop_id, asdict(result))
                stage_originals(workspace, archive, records, ids["attachments"], staged)
                for attachment_id, upload in staged.items():
                    upload.publish(shop_id, storage_name=names[attachment_id])
                if staged:
                    files = blobs.files_root(workspace)
                    blobs.sync_directory(files)
                    blobs.sync_directory(files.parent)
                # Final bytes precede the visibility boundary. Crash leftovers are
                # unreferenced and can be removed by stopped-workspace reconciliation.
                try:
                    db.commit()
                finally:
                    # If commit ended the transaction, preserve bytes even if an
                    # interruption prevented an acknowledgement.
                    committed = not db.in_transaction
            return result
    except storage.DestinationUnavailable as error:
        raise CustomerError(409, "destination_unavailable", str(error)) from None
    except (ArchiveError, RecordError) as error:
        raise CustomerError(422, "invalid_archive", str(error)) from None
    except (WorkspaceError, BadZipFile, ValueError, RecursionError, NotImplementedError,
            StructError, EOFError, OverflowError, blobs.BlobTooLarge) as error:
        raise CustomerError(422, "invalid_archive",
                            "The portable archive contains invalid ZIP or JSON data.") from error
    except OSError as error:
        raise CustomerError(503, "unavailable", "Could not stage or publish the portable originals.") from error
    finally:
        for upload in staged.values():
            if committed:
                upload.keep()
            upload.discard()
