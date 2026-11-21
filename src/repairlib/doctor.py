"""Read-only workspace diagnostics, including stopped-workspace data checks."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from repairlib.config import DISK_RESERVE_BYTES, resolve_workspace
from repairlib.contracts import StockReconciliation
from repairlib.lifecycle.backup_archive import MAX_ATTACHMENTS, RESTORE_STAGE_PREFIX, SHA256, hash_stream, private_file
from repairlib.lifecycle.backup_schedule import backup_line, backup_state
from repairlib.lifecycle.operations import private_subdirectory, workspace_disk_free_bytes
from repairlib.lifecycle.workspace import (
    ACTIVE, STAGE_PREFIX, WorkspaceError, check_database_files, database_path, offline_lock, workspace_schema,
)
from repairlib.storage.blobs import FILES, IMAGE_FOLDER, IMAGES, STAGING, blob_parts
from repairlib.storage.recovery import database_diagnostics, sidecar
from repairlib.storage.sessions import SQLITE_BUSY_TIMEOUT_MS, SQLITE_SYNCHRONOUS

SQLITE_POLICY = {"synchronous": SQLITE_SYNCHRONOUS, "busy_timeout_ms": SQLITE_BUSY_TIMEOUT_MS}
SQLITE_POLICY_LINE = f"SQLite: synchronous={SQLITE_SYNCHRONOUS}, busy timeout={SQLITE_BUSY_TIMEOUT_MS} ms"

MESSAGES = {
    "missing": "Workspace does not exist",
    "uninitialized": "Workspace has not been initialized",
    "interrupted": "Previous setup was interrupted. Setup can be retried",
    "initialized": "Workspace schema is compatible",
    "upgrade_required": "Workspace requires explicit migration",
    "unsupported_schema": "Workspace schema is unsupported",
    "invalid": "Workspace path or database is invalid or inaccessible",
    "maintenance": "Stop the app and other maintenance commands before checking workspace data",
}


def disk_space_line(free_bytes: int | None) -> str:
    free = "unavailable" if free_bytes is None else f"{free_bytes} bytes free"
    return f"Disk space: {free} (reserve: {DISK_RESERVE_BYTES} bytes)"


@dataclass
class DoctorReport:
    status: str
    workspace: Path
    integrity: str = "unchecked"
    foreign_keys: str = "unchecked"
    rollback_journal: bool = False
    setup_staging: int = 0
    restore_staging: int = 0
    attachments_checked: int | None = None
    orphans: int | None = None
    upload_staging: int | None = None
    unsafe_storage: int = 0
    attachment_issues: dict[str, list[int]] = field(default_factory=dict)
    stock: StockReconciliation | None = None
    disk_free_bytes: int | None = None
    backup: dict | None = None

    @property
    def stock_mismatch_ids(self) -> list[int] | None:
        return None if self.stock is None else [mismatch.item_id for mismatch in self.stock.mismatches]

    @property
    def stock_mismatches(self) -> int | None:
        return None if self.stock is None else len(self.stock.mismatches)

    @property
    def message(self) -> str:
        return f"{MESSAGES[self.status]}: {self.workspace}"

    @property
    def exit_status(self) -> int:
        if self.status not in {"initialized", "missing", "uninitialized"}:
            return 1
        if self.status == "initialized" and (
                self.integrity != "ok" or self.foreign_keys != "ok" or self.orphans is None or self.stock is None):
            return 1
        return int(bool(self.setup_staging or self.restore_staging or self.upload_staging
                        or self.orphans or self.unsafe_storage or self.attachment_issues
                        or (self.stock and (self.stock.mismatches or self.stock.issues))))

    def lines(self) -> list[str]:
        unchecked = "unchecked"
        lines = [
            f"{self.status}: {self.message}",
            disk_space_line(self.disk_free_bytes),
            f"SQLite integrity: {self.integrity}",
            f"SQLite foreign keys: {self.foreign_keys}",
            f"Retained attachments checked: {self.attachments_checked if self.attachments_checked is not None else unchecked}",
            f"Orphan entries: {self.orphans if self.orphans is not None else unchecked}",
            f"Upload staging entries: {self.upload_staging if self.upload_staging is not None else unchecked}",
            f"Setup staging entries: {self.setup_staging}",
            f"Restore staging entries in workspace parent: {self.restore_staging}",
            f"Stock ledger mismatches: {self.stock_mismatches if self.stock_mismatches is not None else unchecked}",
            f"Stock ledger issues: {len(self.stock.issues) if self.stock is not None else unchecked}",
        ]
        if self.backup is not None:
            lines.append(backup_line(self.backup))
        if self.attachments_checked is not None and self.attachments_checked >= MAX_ATTACHMENTS * 8 // 10:
            lines.append(f"Warning: {self.attachments_checked} of {MAX_ATTACHMENTS} retained attachments; "
                         "past the backup limit a backup cannot be created. Purge detached files.")
        if self.rollback_journal:
            lines.append("Rollback journal present: yes")
        if self.unsafe_storage:
            lines.append(f"Unsafe or unreadable attachment storage entries: {self.unsafe_storage}")
        for issue, ids in self.attachment_issues.items():
            lines.append(f"{issue} attachment IDs: {', '.join(map(str, ids))}")
        if self.stock_mismatch_ids:
            lines.append(f"Stock ledger mismatch item IDs: {', '.join(map(str, self.stock_mismatch_ids))}")
        if self.stock is not None:
            for issue in self.stock.issues:
                detail = f"shop {issue.shop_id}, movement {issue.movement_id}"
                if issue.item_id is not None:
                    detail += f", item {issue.item_id}"
                if issue.charge_id is not None:
                    detail += f", charge {issue.charge_id}"
                lines.append(f"Stock issue {issue.code} ({detail})")
        return lines


def attachment_issue(files: int, directory: tuple[str, ...], name: str, size: int, digest: str) -> str | None:
    try:
        with private_subdirectory(files, *directory) as parent:
            if parent is None:
                return "Missing"
            with private_file(name, directory=parent) as source:
                if os.fstat(source.fileno()).st_size != size:
                    return "Size mismatch"
                if hash_stream(source, size)["sha256"] != digest:
                    return "SHA-256 mismatch"
    except FileNotFoundError:
        return "Missing"
    except (OSError, WorkspaceError):
        return "Unsafe or unreadable"
    return None


def check_attachments(report: DoctorReport, rows: list[tuple] | None) -> None:
    # Report upload staging even if SQLite didn't provide an attachment inventory.
    with private_subdirectory(report.workspace, ACTIVE, FILES) as files:
        if files is None:
            report.upload_staging = 0
        else:
            try:
                with private_subdirectory(files, STAGING) as staging:
                    report.upload_staging = len(os.listdir(staging)) if staging is not None else 0
            except OSError:
                report.unsafe_storage += 1
        if rows is None:
            return
        known: dict[tuple[str, ...], set[str]] = {}
        report.attachments_checked = len(rows)
        for attachment_id, shop, name, size, digest in rows:
            try:
                if (type(shop) is not int or shop <= 0 or not isinstance(name, str)
                        or type(size) is not int or size <= 0
                        or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
                    raise ValueError
                *folders, filename = blob_parts(shop, name)
                directory = tuple(folders)
            except ValueError:
                issue = "Invalid metadata for"
            else:
                known.setdefault(directory, set()).add(filename)
                name = filename
                issue = "Missing" if files is None else attachment_issue(files, directory, name, size, digest)
            if issue:
                report.attachment_issues.setdefault(issue, []).append(attachment_id)

        report.orphans = 0
        if files is not None:
            for directory in os.listdir(files):
                if directory == STAGING:
                    continue
                if not re.fullmatch(r"shop-[1-9][0-9]*", directory):
                    report.unsafe_storage += 1
                    continue
                try:
                    with private_subdirectory(files, directory) as shop_files:
                        if shop_files is None:
                            continue
                        entries = set(os.listdir(shop_files))
                        if IMAGES in entries:
                            entries.discard(IMAGES)
                            count_image_folders(report, shop_files, directory, known)
                        report.orphans += len(entries - known.get((directory,), set()))
                except OSError:
                    report.unsafe_storage += 1


def count_image_folders(report: DoctorReport, shop_files: int, shop: str, known: dict) -> None:
    with private_subdirectory(shop_files, IMAGES) as images:
        for folder in os.listdir(images):
            if not IMAGE_FOLDER.fullmatch(folder):
                report.unsafe_storage += 1
                continue
            with private_subdirectory(images, folder) as files:
                report.orphans += len(set(os.listdir(files)) - known.get((shop, IMAGES, folder), set()))


def diagnose_workspace(explicit: str | Path | None = None, *, lock_fd: int | None = None) -> DoctorReport:
    workspace = resolve_workspace(explicit)
    report = DoctorReport("invalid", workspace, disk_free_bytes=workspace_disk_free_bytes(workspace),
                          backup=backup_state(workspace))
    try:
        report.restore_staging = sum(1 for _ in workspace.parent.glob(f"{RESTORE_STAGE_PREFIX}*"))
        if workspace.is_symlink():
            return report
        path = database_path(workspace)
        if not os.path.lexists(path.parent):
            report.status, _ = workspace_schema(workspace)
            if workspace.is_dir():
                report.setup_staging = sum(1 for _ in workspace.glob(f"{STAGE_PREFIX}*"))
            return report
        if path.parent.is_symlink():
            return report

        with offline_lock(workspace, lock_fd):
            report.setup_staging = sum(1 for _ in workspace.glob(f"{STAGE_PREFIX}*"))
            check_database_files(path)
            report.status, version = workspace_schema(workspace)
            report.rollback_journal = sidecar(path, "-journal").exists()
            rows = None
            if not report.rollback_journal:
                report.integrity, report.foreign_keys, rows, report.stock = database_diagnostics(
                    path, version if report.status in {"initialized", "upgrade_required"} else None)
            check_attachments(report, rows)
    except WorkspaceError:
        report.status = "maintenance"
    except OSError:
        report.status = "invalid"
    return report
