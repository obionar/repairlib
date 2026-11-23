"""Offline, recoverable updates for a checkout with a registered service."""

import json
import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp

from repairlib.config import DISK_RESERVE_BYTES
from repairlib.doctor import diagnose_workspace
from repairlib.lifecycle.backup_archive import archived_workspace, restore_archive, verify_restore_point
from repairlib.lifecycle.checkout import (
    PENDING, READY, CheckoutError, checkout_state, fetch_update, git, service_command, service_target,
)
from repairlib.lifecycle.service import check_path, ignored_in_checkout, overlaps
from repairlib.lifecycle.workspace import WorkspaceError, maintenance_lock, migrate_workspace
from repairlib.paths import sync_directory

BOOT = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from repairlib.lifecycle.checkout_update import check_candidate; check_candidate(*sys.argv[1:])"
)


def checked_doctor(workspace: Path, lock_fd: int | None) -> None:
    report = diagnose_workspace(workspace, lock_fd=lock_fd)
    if (report.status != "initialized" or report.exit_status or report.rollback_journal
            or (report.disk_free_bytes or 0) < DISK_RESERVE_BYTES):
        raise CheckoutError("\n".join(report.lines()))


def check_candidate(workspace: str, backup: str, fd: str = "") -> None:
    """Run in the new revision, never mix old Python imports with new migrations."""
    lock_fd = int(fd) if fd else None
    path = Path(workspace)
    try:
        migrate_workspace(path, check_backup=lambda schema: verify_restore_point(Path(backup), schema, workspace=path),
                          lock_fd=lock_fd)
        checked_doctor(path, lock_fd)
    except (CheckoutError, WorkspaceError, OSError) as error:
        raise SystemExit(str(error)) from error


def run_candidate(repo: Path, workspace: Path, backup: Path, lock_fd: int | None = None) -> None:
    args = [sys.executable, "-I", "-B", "-c", BOOT, str(repo / "src"), str(workspace), str(backup)]
    fds = () if lock_fd is None else (lock_fd,)
    done = subprocess.run(args + [str(fd) for fd in fds], capture_output=True, text=True, pass_fds=fds)
    if done.returncode:
        raise CheckoutError(done.stderr.strip() or "The new revision did not pass migration and doctor.")


def journal(path: Path, state: dict, stage: str) -> None:
    state["stage"] = stage
    with NamedTemporaryFile(dir=path.parent, prefix=".repairlib-update-", delete=False) as file:
        temp = Path(file.name)
        try:
            file.write((json.dumps(state, indent=2) + "\n").encode())
            file.flush()
            os.fsync(file.fileno())
            temp.replace(path)
            sync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)


def recovery_message(path: Path) -> str:
    return (f"Recovery record: {path}. Do not pull, retry the update, or start the service yet. "
            "Follow the interrupted checkout update instructions in docs/operations.md. "
            "No data was restored automatically.")


def stopped_or_running(repo: Path, target: tuple) -> str:
    observed = service_command(repo, "status", target).get("service")
    if observed not in {"running", "unready", "stopped"}:
        raise CheckoutError("The managed service is not confirmed running or stopped.")
    return observed


def validate_workspace(repo: Path, workspace: Path, after: str) -> None:
    check_path(workspace)
    if repo.is_relative_to(workspace) or overlaps(repo / ".git", workspace) or overlaps(repo / "src", workspace):
        raise CheckoutError("The workspace overlaps the checkout runtime or Git metadata.")
    if workspace.is_relative_to(repo):
        if not ignored_in_checkout(workspace, repo):
            raise CheckoutError("The workspace inside this checkout must be Git-ignored.")
        relative = workspace.relative_to(repo)
        # A tracked file, directory, symlink or submodule at the workspace path
        # (or a non-directory ancestor) could change which data the service sees.
        for path in (relative, *relative.parents):
            if path == Path("."):
                break
            entry = git(repo, "--literal-pathspecs", "ls-tree", after, "--", path.as_posix())
            if entry and (path == relative or not entry.startswith("040000 tree ")):
                raise CheckoutError("The incoming commit overlaps the workspace path.")


def update_checkout(repo: Path, *, progress: Callable[[str], None] | None = None) -> tuple[str, str, str, Path | None]:
    metadata = repo / ".git"
    if (not metadata.is_dir() or metadata.is_symlink()
            or git(repo, "rev-parse", "--absolute-git-dir") != str(metadata)):
        raise CheckoutError("Automatic updates require a regular Git checkout, not a linked worktree.")
    pending = metadata / PENDING
    # Separate from the workspace lock: held before service stop and through its restart.
    with maintenance_lock(metadata, exclusive=True) as update_fd:
        if os.path.lexists(pending):
            raise CheckoutError(f"A previous update did not finish. {recovery_message(pending)}")
        branch, before = checkout_state(repo)
        after = fetch_update(repo, branch, before)
        if before == after:
            return before, after, "unchanged", None
        target = service_target()
        workspace, port, openrc, *_ = target
        validate_workspace(repo, workspace, after)
        restart = stopped_or_running(repo, target) != "stopped"
        recovery = Path(mkdtemp(prefix=".repairlib-recovery-", dir=repo.parent))
        backup = recovery / "backup.zip"
        state = {"old_commit": before, "new_commit": after, "workspace": str(workspace), "port": port,
                 "backend": "openrc" if openrc else "systemd", "restart": restart, "backup": str(backup)}
        stage = "stop service"

        def advance(value: str) -> None:
            nonlocal stage
            stage = value
            journal(pending, state, value)
            if progress is not None:
                progress(value)

        try:
            sync_directory(recovery.parent)
            advance(stage)
            if restart and service_command(repo, "stop", target).get("service") != "stopped":
                raise CheckoutError("The supervisor did not confirm the service stopped.")
            with maintenance_lock(workspace, exclusive=True, create=False) as lock_fd:
                advance("pre-update doctor")
                checked_doctor(workspace, lock_fd)
                advance("create and verify full backup")
                with archived_workspace(workspace, backup, lock_fd=lock_fd):
                    sync_directory(recovery)
                advance("rehearse restore, migration and doctor")
                with TemporaryDirectory(prefix="rehearsal-", dir=recovery) as temporary:
                    rehearsal = Path(temporary)
                    copy = rehearsal / "workspace"
                    restore_archive(backup, copy)
                    candidate = rehearsal / "checkout"
                    git(repo, "clone", "--quiet", "--shared", "--no-checkout", "--", str(repo), str(candidate))
                    git(candidate, "checkout", "--quiet", "--detach", after)
                    if workspace.is_relative_to(repo):
                        relative = workspace.relative_to(repo).as_posix() + "/"
                        try:
                            git(candidate, "check-ignore", "--quiet", "--", relative)
                        except CheckoutError as error:
                            raise CheckoutError("The incoming checkout does not ignore the workspace.") from error
                    run_candidate(candidate, copy, backup)
                advance("fast-forward checkout")
                if checkout_state(repo) != (branch, before):
                    raise CheckoutError("The checkout changed during rehearsal.")
                validate_workspace(repo, workspace, after)
                git(repo, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", "--quiet", after,
                    pass_fds=(lock_fd, update_fd))
                if checkout_state(repo) != (branch, after):
                    raise CheckoutError("Could not confirm the fast-forwarded checkout.")
                advance("verify service registration with new checkout")
                if stopped_or_running(repo, target) != "stopped":
                    raise CheckoutError("The service is no longer stopped.")
                advance("migrate and run post-update doctor")
                run_candidate(repo, workspace, backup, lock_fd)
            if restart:
                advance(READY)
                report = service_command(repo, "start", target)
                if report.get("service") != "running" or report.get("health") != "ready":
                    raise CheckoutError("The service listener and health checks did not pass.")
            pending.unlink()
            sync_directory(metadata)
        except (Exception, KeyboardInterrupt) as error:
            detail = "Interrupted." if isinstance(error, KeyboardInterrupt) else str(error)
            if stage == READY:
                # Doctor already passed, but do not leave a failed start serving traffic.
                with suppress(Exception, KeyboardInterrupt):
                    journal(pending, state, "service start or verification failed")
                try:
                    if service_command(repo, "stop", target).get("service") != "stopped":
                        raise CheckoutError("the supervisor did not confirm a stop")
                except (Exception, KeyboardInterrupt) as stop_error:
                    detail += f"\nCould not confirm a stop after failed startup ({stop_error}); check service status."
            archive = f"Backup location: {backup} (verify before use; it may not exist yet)."
            raise CheckoutError(f"Stopped at {stage}: {detail}\n{archive}\n{recovery_message(pending)}") from error
        return before, after, "ready" if restart else "left stopped", backup
