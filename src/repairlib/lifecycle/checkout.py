"""Git and registered-service discovery for checkout updates."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

PENDING = "repairlib-update-pending.json"
READY = "start and verify service"


class CheckoutError(Exception):
    """The checkout cannot be updated safely."""


def git(repo: Path, *arguments: str, pass_fds: tuple[int, ...] = ()) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                              pass_fds=pass_fds)
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


def current_branch(repo: Path) -> str:
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise CheckoutError("The checkout is on a detached HEAD; check out a branch first.")
    return branch


def remote_commit(repo: Path) -> str:
    """Get the origin commit for the current branch without changing the checkout."""
    branch = current_branch(repo)
    out = git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    if not out:
        raise CheckoutError(f"Branch {branch!r} does not exist on origin.")
    return out.split()[0][:7]


def checkout_state(repo: Path) -> tuple[str, str]:
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise CheckoutError("The checkout has uncommitted changes; commit or move them first.")
    if any(entry.startswith("S ") or entry[:1].islower()
           for entry in git(repo, "ls-files", "-v", "-z").split("\0")):
        raise CheckoutError("Clear Git skip-worktree and assume-unchanged flags before updating.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer"):
        if (repo / ".git" / marker).exists():
            raise CheckoutError("Finish the Git operation already in progress before updating.")
    return current_branch(repo), git(repo, "rev-parse", "HEAD")


def fetch_update(repo: Path, branch: str, before: str) -> str:
    """Fetch the current origin branch, checking the exact commit we'll rehearse."""
    git(repo, "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{branch}")
    after = git(repo, "rev-parse", "FETCH_HEAD^{commit}")
    base = git(repo, "merge-base", before, after)
    if base == after:
        return before
    if base != before:
        raise CheckoutError("Local and origin histories diverged; reconcile them before updating.")
    return after


def parse_service_target(text: str) -> tuple[Path, int] | None:
    """Workspace and port from a unit or init script, when both are present."""
    for line in text.splitlines():
        try:
            if line.startswith("ExecStart="):
                args = shlex.split(line.removeprefix("ExecStart=").removeprefix(":"))
                args = [arg.replace("%%", "%") for arg in args]
            elif line.startswith("command_args="):
                words, = shlex.split(line.removeprefix("command_args="))
                args = shlex.split(words)
            else:
                continue
            workspace = args[args.index("--workspace") + 1]
            port = int(args[args.index("--port") + 1])
            return Path(workspace), port
        except (ValueError, IndexError):
            return None
    return None


def service_target() -> tuple[Path, int, bool, bool]:
    """Discover arguments only; the service CLI separately verifies exact registration."""
    from repairlib.lifecycle.service import OPENRC_INITD, OPENRC_SERVICE, UNIT, using_openrc

    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    candidates = [(config / "systemd" / "user" / UNIT, False), (OPENRC_INITD / OPENRC_SERVICE, True)]
    targets = []
    for path, openrc in candidates:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or not path.is_file() or info.st_size > 65536:
            raise CheckoutError("Refusing an invalid service registration.")
        text = path.read_text(encoding="utf8")
        parsed = parse_service_target(text)
        if parsed is None:
            raise CheckoutError("Could not read workspace and port from the registered service.")
        lan = re.search(r"(?m)^(?:ExecStart|command_args)=.*\s--lan\b", text) is not None
        targets.append((*parsed, openrc, lan))
    if len(targets) != 1 or (not targets[0][2] and using_openrc()):
        raise CheckoutError("Automatic checkout updates require exactly one managed OpenRC or systemd user service.")
    return targets[0]


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


def service_command(repo: Path, action: str, target: tuple) -> dict:
    """Use the service CLI's identity, supervisor, listener and health checks."""
    workspace, port, openrc, *lan = target  # (workspace, port, openrc[, lan])
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(repo / "bin" / "repairlib"), "service", action,
         "--workspace", str(workspace), "--port", str(port)] + (["--openrc-system"] if openrc else [])
        + (["--lan"] if any(lan) else []),
        capture_output=True, text=True,
    )
    try:
        report = json.loads(done.stdout)
    except ValueError:
        report = None
    if done.returncode or not isinstance(report, dict):
        detail = report.get("error") if isinstance(report, dict) else None
        raise CheckoutError(done.stderr.strip() or detail or f"Could not confirm service {action}.")
    return report
