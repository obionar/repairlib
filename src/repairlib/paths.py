"""Workspace file layout shared by storage, domain and lifecycle (a leaf module)."""
import os
import stat
from pathlib import Path

ACTIVE = "state"
DATABASE = "repairlib.sqlite3"


def database_path(workspace: Path) -> Path:
    return workspace / ACTIVE / DATABASE


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_database_files(path: Path) -> None:
    """Refuse aliases and special files before handing the database to SQLite."""
    for candidate in (path, *(path.with_name(path.name + suffix)
                              for suffix in ("-wal", "-shm", "-journal"))):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if candidate == path:
                raise
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Workspace database files must be regular files without links.")
