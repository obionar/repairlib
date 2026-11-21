"""Stopped-workspace snapshots and read-only recovery verification."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from repairlib.lifecycle.operations import check_database_files
from repairlib.lifecycle.workspace import DATABASE, WorkspaceError, database_path, maintenance_lock, offline_lock
from repairlib.storage.recovery import (
    SIDECAR_SUFFIXES, SnapshotError, copy_database, online_copy_database, sidecar,
    verify_database_snapshot,
)


def require_private(info: os.stat_result, kind) -> None:
    if os.name != "posix":
        raise WorkspaceError("Secure backups currently require POSIX file permissions.")
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise WorkspaceError("Backup paths must be private and owned by you.")


def require_no_sidecars(path: Path) -> None:
    if any(os.path.lexists(sidecar(path, suffix)) for suffix in SIDECAR_SUFFIXES):
        raise WorkspaceError("Snapshot path must be free of SQLite sidecars.")


def verify_snapshot(path: Path) -> int:
    require_private(path.parent.lstat(), stat.S_ISDIR)
    require_private(path.lstat(), stat.S_ISREG)
    require_no_sidecars(path)
    try:
        return verify_database_snapshot(path.absolute())
    except SnapshotError as error:
        raise WorkspaceError(str(error)) from error


def publish_backup(staged: Path, output: Path) -> None:
    with staged.open("rb") as handle:
        os.fsync(handle.fileno())
    try:
        os.link(staged, output)
    except FileExistsError as error:
        raise WorkspaceError("Backup output already exists.") from error
    except OSError as error:
        raise WorkspaceError("Could not publish the backup.") from error


@contextmanager
def staged_workspace_snapshot(workspace: Path, output: Path, *, database_only: bool,
                              online: bool = False, lock_fd: int | None = None):
    """Yield a verified snapshot while holding the lock through publication."""
    if workspace.is_symlink() or not workspace.is_dir():
        raise WorkspaceError("Backup creation requires an existing workspace directory.")
    if output.parent.is_symlink():
        raise WorkspaceError("Backup output must have a real, private parent directory.")
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    if output.is_relative_to(workspace.resolve(strict=True)):
        raise WorkspaceError("Backup output must be outside the source workspace.")
    require_private(parent.stat(), stat.S_ISDIR)
    if os.path.lexists(output):
        raise WorkspaceError("Backup output already exists.")
    if database_only:
        require_no_sidecars(output)

    if online and lock_fd is not None:
        raise ValueError("An inherited workspace lock is only for offline backups.")
    lock = maintenance_lock(workspace, exclusive=False, create=False) if online else offline_lock(workspace, lock_fd)
    with lock:
        source = database_path(workspace)
        require_private(source.parent.lstat(), stat.S_ISDIR)
        check_database_files(source)
        if sidecar(source, "-journal").exists():
            raise WorkspaceError("Resolve the workspace SQLite rollback journal before taking a snapshot.")

        with TemporaryDirectory(prefix=".snapshot-", dir=parent) as temporary:
            staged = Path(temporary) / DATABASE
            staged.touch(mode=0o600, exist_ok=False)
            try:
                (online_copy_database if online else copy_database)(source.absolute(), staged)
            except SnapshotError as error:
                raise WorkspaceError(str(error)) from error
            yield staged, verify_snapshot(staged), output


def snapshot_workspace(workspace: Path, output: Path, *, online: bool = False) -> int:
    with staged_workspace_snapshot(workspace, output, database_only=True,
                                   online=online) as (staged, version, output):
        publish_backup(staged, output)
        return version
