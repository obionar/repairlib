"""Keep a source checkout current and restart its service afterwards."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess

UNIT = "repairlib.service"
OPENRC_SCRIPT = Path("/etc/init.d/repairlib")
ARGUMENTS = re.compile(r"--workspace\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+)).*?--port\s+(\d+)")


class CheckoutError(Exception):
    """The checkout cannot be updated safely."""


def git(repo: Path, *arguments: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True)
    except OSError as error:
        raise CheckoutError("git is not available.") from error
    if done.returncode:
        raise CheckoutError(done.stderr.strip() or f"git {arguments[0]} failed.")
    return done.stdout.strip()


def current_commit(repo: Path) -> str | None:
    try:
        return git(repo, "rev-parse", "--short", "HEAD")
    except CheckoutError:
        return None


def remote_commit(repo: Path, branch: str = "main") -> str | None:
    """The commit this branch would update to, without changing the checkout."""
    try:
        out = git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    except CheckoutError:
        return None
    return out.split()[0][:7] if out else None


def pull(repo: Path) -> tuple[str, str]:
    """Fetch and fast-forward this checkout; return the old and new commits."""
    if git(repo, "status", "--porcelain"):
        raise CheckoutError("The checkout has uncommitted changes; commit or discard them first.")
    before = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise CheckoutError("The checkout is on a detached HEAD; check out a branch first.")
    git(repo, "fetch", "--quiet", "origin")
    git(repo, "merge", "--ff-only", "--quiet", f"origin/{branch}")
    return before, git(repo, "rev-parse", "HEAD")


def parse_service_target(text: str) -> tuple[Path, int] | None:
    """Workspace and port from a unit or init script, when both are present."""
    match = ARGUMENTS.search(text)
    if match is None:
        return None
    return Path(next(group for group in match.groups()[:3] if group)), int(match.group(4))


def service_target() -> tuple[Path, int, bool] | None:
    """Workspace, port and backend of a registered service, when one can be read."""
    candidates = [(Path.home() / ".config" / "systemd" / "user" / UNIT, False), (OPENRC_SCRIPT, True)]
    for path, openrc in candidates:
        try:
            parsed = parse_service_target(path.read_text(encoding="utf8"))
        except OSError:
            continue
        if parsed is not None:
            return *parsed, openrc
    return None


@lru_cache(maxsize=1)
def version_text() -> str:
    """Distribution version plus the checkout commit, when the running copy has one."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        base = version("repairlib")
    except PackageNotFoundError:
        base = "0.0.0"
    try:
        commit = current_commit(Path(__file__).resolve().parents[3])
    except Exception:  # a read-only or foreign checkout must not break --help
        commit = None
    return f"repairlib {base}" + (f" ({commit})" if commit else "")


def restart_service(workspace: Path, port: int, openrc: bool) -> None:
    """Restart the registered service through the existing service command."""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    done = subprocess.run(["/usr/bin/python3", "-m", "repairlib", "service", "restart",
                           "--workspace", str(workspace), "--port", str(port)]
                          + (["--openrc-system"] if openrc else []),
                          capture_output=True, text=True, env=environment)
    if done.returncode:
        try:
            report = json.loads(done.stdout)
        except (ValueError, TypeError):
            report = None
        detail = report.get("error") if isinstance(report, dict) else None
        raise CheckoutError(done.stderr.strip() or detail or "The service did not restart.")
