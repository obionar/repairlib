"""Workspace file layout shared by storage, domain and lifecycle (a leaf module)."""
from pathlib import Path

ACTIVE = "state"
DATABASE = "repairlib.sqlite3"


def database_path(workspace: Path) -> Path:
    return workspace / ACTIVE / DATABASE
