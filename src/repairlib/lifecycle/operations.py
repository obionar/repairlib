"""Owner-only status, cleanup and synthetic demo reset for local workspaces."""

from contextlib import ExitStack, closing, contextmanager
import os
from pathlib import Path
import re
import shutil
import stat
import time
from datetime import UTC, datetime

from repairlib.config import DISK_RESERVE_BYTES
from repairlib.lifecycle.workspace import (
    ACTIVE, WorkspaceError, database_path, maintenance_lock, workspace_schema, workspace_status,
)
from repairlib.paths import check_database_files
from repairlib.storage.blobs import FILES, STAGING, UPLOAD_PART
from repairlib.storage.initialization import SCHEMA_VERSION
from repairlib.storage.sessions import connect
from repairlib.storage.workspace_records import WorkspaceDataError, reset_demo_records, workspace_counts

STALE_AFTER_SECONDS = 3600
RECEIPT_RETENTION_SECONDS = 90 * 24 * 60 * 60
RECEIPT_TABLES = (
    "attachment_requests", "charge_requests", "csv_requests", "customer_requests",
    "inventory_requests", "portable_imports", "printer_requests", "repair_requests", "settings_requests",
    "stock_requests", "user_requests",
)
PERMANENT = {"stock_requests": " AND NOT (movement_id IS NOT NULL AND operation IN "
             "('inventory.consume', 'inventory.return', 'inventory.intake_mistake'))"}


def workspace_disk_free_bytes(workspace: Path) -> int | None:
    try:
        if not stat.S_ISDIR(workspace.lstat().st_mode):
            return None
        # Probe the same filesystem as uploads once active state exists.
        state = database_path(workspace).parent
        try:
            mode = state.lstat().st_mode
        except FileNotFoundError:
            return shutil.disk_usage(workspace).free
        if stat.S_ISDIR(mode):
            return shutil.disk_usage(state).free
    except OSError:
        pass
    return None


def local_status(workspace: Path, *, writes_closed: bool = False) -> dict:
    status, version = workspace_schema(workspace)
    report = {
        "workspace": str(workspace),
        "schema_version": version,
        "expected_schema_version": SCHEMA_VERSION,
        "disk_free_bytes": workspace_disk_free_bytes(workspace),
        "disk_reserve_bytes": DISK_RESERVE_BYTES,
        "customers": None,
        "repairs": None,
        "demo_seeded": None,
    }
    if status == "initialized":
        with ExitStack() as locks:
            stopped = True
            try:
                try:
                    locks.enter_context(maintenance_lock(workspace, exclusive=True, create=False))
                except WorkspaceError:
                    stopped = False
                    locks.enter_context(maintenance_lock(workspace, exclusive=False))
                status, report["schema_version"] = workspace_schema(workspace)
                if status == "initialized":
                    path = database_path(workspace)
                    check_database_files(path)
                    # Closed selections hold the installation lock during diagnostics.
                    # Their health-only listener cannot create a WAL during this read.
                    report.update(workspace_counts(path, stopped=stopped or writes_closed))
            except WorkspaceError:
                status = "maintenance"
            except (OSError, WorkspaceDataError):
                status = "invalid"
    return {**report, "status": status, "ready": status == "initialized"}


@contextmanager
def private_subdirectory(root: Path | int, *names: str):
    """Keep the opened path anchored and refuse symlinks at each component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        fd = os.dup(root) if isinstance(root, int) else os.open(root, flags)
        stack.callback(os.close, fd)
        for name in names:
            try:
                fd = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                yield None
                return
            stack.callback(os.close, fd)
        yield fd


def prune_workspace_records(path: Path, now: int) -> tuple[int, int]:
    """Delete expired sessions and replay receipts past the retention window."""
    cutoff = datetime.fromtimestamp(now - RECEIPT_RETENTION_SECONDS, UTC).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    with closing(connect(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        sessions = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount
        # Static table names only; receipts are keyed by shop and actor, so no join is needed.
        # Blank dates are unknown, not old. Stock proof and the import boundary are evidence
        # that `doctor` re-derives from, so they never expire (audit R02).
        receipts = sum(db.execute(
            f"DELETE FROM {table} WHERE created_at <> '' AND created_at < ?{PERMANENT.get(table, '')}",
            (cutoff,)).rowcount for table in RECEIPT_TABLES if table != "portable_imports")
    return sessions, receipts


def cleanup_workspace(workspace: Path) -> dict[str, int]:
    empty = {"staging": 0, "sessions": 0, "receipts": 0}
    if workspace.is_symlink():
        raise WorkspaceError("Workspace must be a real directory.")
    if not os.path.lexists(workspace):
        return empty
    if not workspace.is_dir():
        raise WorkspaceError("Workspace must be a real directory.")
    with maintenance_lock(workspace, exclusive=True, create=False):
        if workspace_status(workspace) != "initialized":
            raise WorkspaceError("Workspace must be initialized and use the current schema.")
        now = time.time()
        stale = []
        with private_subdirectory(workspace, ACTIVE, FILES, STAGING) as staging:
            if staging is not None:
                cutoff = now - STALE_AFTER_SECONDS
                for name in os.listdir(staging):
                    if not UPLOAD_PART.fullmatch(name):
                        continue
                    info = os.stat(name, dir_fd=staging, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mtime <= cutoff:
                        stale.append(name)
            # An unsafe or unreadable staging path is a refusal, not a partial
            # cleanup that already pruned sessions or request receipts.
            sessions, receipts = prune_workspace_records(database_path(workspace), int(now))
            if staging is not None:
                for name in stale:
                    os.unlink(name, dir_fd=staging)
            return {"staging": len(stale), "sessions": sessions, "receipts": receipts}


def reset_demo_workspace(workspace: Path) -> None:
    if workspace.is_symlink() or workspace_status(workspace) != "initialized":
        raise WorkspaceError("Demo reset requires an existing workspace with the current schema.")
    with maintenance_lock(workspace, exclusive=True, create=False):
        if workspace_status(workspace) != "initialized":
            raise WorkspaceError("Demo reset requires an existing workspace with the current schema.")
        path = database_path(workspace)
        check_database_files(path)
        with private_subdirectory(workspace, ACTIVE, FILES) as files:
            if files is not None:
                for name in os.listdir(files):
                    if name == STAGING or re.fullmatch(r"shop-[1-9][0-9]*", name):
                        with private_subdirectory(files, name) as directory:
                            if directory is not None and not os.listdir(directory):
                                continue
                    # The seed owns no attachment bytes. Even orphans are outside reset.
                    raise WorkspaceError("Demo reset refuses a workspace with attachment storage content.")
        try:
            reset_demo_records(path)
        except WorkspaceDataError as error:
            raise WorkspaceError(str(error)) from error
