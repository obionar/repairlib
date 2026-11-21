"""Stable workspace lock, staged initialization and atomic publication."""

from __future__ import annotations

import os
from secrets import token_bytes
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from repairlib.paths import ACTIVE, DATABASE, database_path  # noqa: F401  (re-exported for lifecycle callers)
from repairlib.storage.initialization import (
    UpgradeError,
    initialize_database,
    schema_details,
    upgrade_database,
)

try:
    import fcntl
except ImportError:
    fcntl = None

LOCK = ".maintenance.lock"
STAGE_PREFIX = ".setup-"
SETUP_STATES = frozenset({"missing", "uninitialized", "interrupted"})


class WorkspaceError(Exception):
    """A local workspace operation cannot proceed."""


def workspace_schema(workspace: Path) -> tuple[str, int | None]:
    if not workspace.is_dir():
        return ("invalid" if os.path.lexists(workspace) else "missing"), None
    active = workspace / ACTIVE
    if os.path.lexists(active):
        db = database_path(workspace)
        if active.is_symlink() or db.is_symlink() or not db.is_file():
            return "invalid", None
        return schema_details(db)
    if any(workspace.glob(f"{STAGE_PREFIX}*")):
        return "interrupted", None
    return "uninitialized", None


def workspace_status(workspace: Path) -> str:
    return workspace_schema(workspace)[0]


@contextmanager
def maintenance_lock(workspace: Path, *, exclusive: bool, create: bool = True) -> Iterator[None]:
    if fcntl is None:
        raise WorkspaceError("Workspace locking is unavailable on this platform.")
    flags = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    if exclusive and create:
        flags |= os.O_WRONLY | os.O_CREAT
    else:
        flags |= os.O_RDONLY
    fd = os.open(workspace / LOCK, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError("Workspace lock is invalid.")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceError("Workspace maintenance is in progress or the app is running.") from error
        yield
    finally:
        os.close(fd)


def load_form_key(workspace: Path) -> bytes:
    path = workspace / ".form-key"
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            with NamedTemporaryFile(dir=workspace, prefix=".form-key-") as pending:
                os.fchmod(pending.fileno(), 0o600)
                pending.write(token_bytes(32))
                pending.flush()
                os.fsync(pending.fileno())
                # Publish complete bytes without overwriting another startup's key.
                try:
                    os.link(pending.name, path)
                except FileExistsError:
                    pass
            fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 32):
                raise WorkspaceError("Invalid workspace form key.")
            key = file.read(33)
            if len(key) != 32:
                raise WorkspaceError("Invalid workspace form key.")
            return key
    except OSError as error:
        raise WorkspaceError("Cannot access the workspace form key.") from error


def publish_state(stage: Path, active: Path) -> None:
    stage.rename(active)


def create_workspace(
    workspace: Path, shop: str, timezone: str, username: str, password_hash: str,
    *, demo: bool = False, language: str = "",
) -> None:
    if fcntl is None:
        raise WorkspaceError("Workspace locking is unavailable on this platform.")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    with maintenance_lock(workspace, exclusive=True):
        status = workspace_status(workspace)
        if status == "initialized":
            raise WorkspaceError("Workspace is already initialized.")
        if status == "unsupported_schema":
            raise WorkspaceError("Workspace schema is unsupported.")
        if status == "upgrade_required":
            raise WorkspaceError("Workspace schema requires explicit migration.")
        if status not in {"uninitialized", "interrupted"}:
            raise WorkspaceError("Workspace state is invalid.")
        with TemporaryDirectory(prefix=STAGE_PREFIX, dir=workspace) as temp:
            stage = Path(temp) / ACTIVE
            stage.mkdir(mode=0o700)
            initialize_database(stage / DATABASE, shop, timezone, username, password_hash, demo=demo,
                              language=language)
            publish_state(stage, workspace / ACTIVE)


def migrate_workspace(workspace: Path, *, check_backup: Callable[[int], str] | None = None) -> str | None:
    """Upgrade explicitly; `check_backup` runs under the lock, before any write, with the current schema."""
    if workspace_status(workspace) not in {"initialized", "upgrade_required"}:
        raise WorkspaceError("Workspace schema is unavailable or unsupported.")
    with maintenance_lock(workspace, exclusive=True):
        status = workspace_status(workspace)
        if status == "upgrade_required":
            verified = None
            if check_backup is not None:
                version = schema_details(database_path(workspace))[1]
                if version is None:
                    raise WorkspaceError("The workspace schema cannot be read to match a backup.")
                verified = check_backup(version)
            try:
                upgrade_database(database_path(workspace))
            except UpgradeError as error:
                raise WorkspaceError(str(error)) from error
            return verified
        elif status != "initialized":
            raise WorkspaceError("Workspace schema is unavailable or unsupported.")
        return None
