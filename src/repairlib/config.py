"""Workspace and state path resolution.

Path resolution is pure: it never creates directories and never opens a
database. An explicit workspace path is supported for tests and multiple
instances; otherwise the path is platform-appropriate and stable under the
``repairlib`` identifier.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_IDENTIFIER = "repairlib"
DISK_RESERVE_BYTES = 1 << 30
LOOPBACK_BIND_HOSTS = ("127.0.0.1", "localhost", "::1")


def default_state_root() -> Path:
    """Return the default persistent state root without creating it."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_IDENTIFIER


def configured_base_url() -> str | None:
    """The HTTP base URL an agent was pointed at, if the environment sets one."""
    return os.environ.get("REPAIRLIB_BASE_URL") or None


def resolve_workspace(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a workspace path; an explicit path wins over the default."""
    if explicit is None:
        return default_state_root()
    path = Path(explicit).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path
