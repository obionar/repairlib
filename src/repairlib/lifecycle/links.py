"""Install the `repairlib` command and the agent skill for this account."""

from __future__ import annotations

import os
from pathlib import Path

#: Directory that holds `src/`, `bin/`, `skills/` and `docs/` (a checkout or a staged generation).
APP_ROOT = Path(__file__).resolve().parents[3]


class LinkError(Exception):
    """A link cannot be created without touching an unrelated file."""


def default_executable(root: Path = APP_ROOT) -> Path:
    """The launcher for this tree: a staged generation is run through its stable launcher."""
    if root.parent.name == "generation":
        return root.parents[1] / "repairlib"
    return root / "bin" / "repairlib"


def _place(link: Path, destination: Path) -> str:
    """Create one symlink; replace only a symlink, never a real file or directory."""
    if link.is_symlink():
        if os.readlink(link) == str(destination):
            return "unchanged"
        link.unlink()
    elif os.path.lexists(link):
        return "blocked"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(destination)
    return "created"


def install_links(executable: Path, skills: Path, home: Path) -> dict:
    """Link the launcher into `~/.local/bin` and the skill into `~/.agents/skills`."""
    executable = executable.expanduser().resolve()
    skills = skills.expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LinkError(f"{executable} is not an executable file.")
    if not (skills / "SKILL.md").is_file():
        raise LinkError(f"{skills} does not contain SKILL.md.")
    home = home.expanduser()
    targets = {
        "command": (home / ".local" / "bin" / "repairlib", executable),
        "skill": (home / ".agents" / "skills" / "repairlib", skills),
    }
    return {name: {"path": str(link), "target": str(destination), "status": _place(link, destination)}
            for name, (link, destination) in targets.items()}
