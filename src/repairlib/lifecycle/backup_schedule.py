"""Daily online snapshots with on-device retention and staleness reporting."""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from uuid import uuid4

from repairlib.lifecycle.recovery import snapshot_workspace, verify_snapshot
from repairlib.lifecycle.workspace import WorkspaceError

BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_RETENTION_SECONDS = 14 * 24 * 60 * 60
BACKUP_STALE_SECONDS = 26 * 60 * 60
SNAPSHOT_NAME = re.compile(r"repairlib-\d{8}-\d{6}(?:-[0-9a-f]{32})?\.sqlite3")


def backup_directory(workspace: Path) -> Path:
    """Snapshots sit beside the workspace, never inside the tree they copy."""
    return workspace.parent / f"{workspace.name}-backups"


def snapshots(directory: Path) -> list[Path]:
    """Regular snapshot files in *directory*, newest first."""
    try:
        found = [Path(entry.path) for entry in os.scandir(directory)
                 if SNAPSHOT_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)]
        return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []


def backup_state(workspace: Path, *, now: int | None = None) -> dict:
    moment = int(time.time()) if now is None else now
    directory = backup_directory(workspace)
    taken = None
    for newest in snapshots(directory):
        try:
            before = newest.stat()
            if int(before.st_mtime) > moment:
                continue
            verify_snapshot(newest)
            after = newest.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                taken = after.st_mtime
                break
        except (OSError, WorkspaceError):
            continue
    if taken is None:
        return {"directory": str(directory), "last_verified_at": None, "age_seconds": None, "stale": True}
    age = max(0, moment - int(taken))
    return {
        "directory": str(directory),
        "last_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(taken)),
        "age_seconds": age,
        "stale": age > BACKUP_STALE_SECONDS,
    }


def backup_line(state: dict) -> str:
    if state["last_verified_at"] is None:
        return "Last verified backup: none"
    line = f"Last verified backup: {state['last_verified_at']} ({state['age_seconds'] // 3600} h ago)"
    return line + (" — stale" if state["stale"] else "")


def scheduled_snapshot(workspace: Path, *, now: int | None = None) -> Path | None:
    """Take the due daily snapshot; return its path, or None when one is already current."""
    moment = int(time.time()) if now is None else now
    state = backup_state(workspace, now=moment)
    if state["age_seconds"] is not None and state["age_seconds"] < BACKUP_INTERVAL_SECONDS:
        return None
    directory = backup_directory(workspace)
    directory.mkdir(mode=0o700, exist_ok=True)
    output = directory / f"repairlib-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(moment))}-{uuid4().hex}.sqlite3"
    snapshot_workspace(workspace, output, online=True)
    retain(directory, moment)
    return output


def retain(directory: Path, now: int) -> None:
    """Delete snapshots past the retention window, always keeping the newest."""
    cutoff = now - BACKUP_RETENTION_SECONDS
    for path in snapshots(directory)[1:]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
