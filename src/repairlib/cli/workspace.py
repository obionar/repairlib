"""Workspace-level commands: setup, health, backup and maintenance. Each handler takes parsed args."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from repairlib.cli.common import add_workspace, read_password
from repairlib.config import LOOPBACK_BIND_HOSTS, resolve_workspace
from repairlib.doctor import SQLITE_POLICY, SQLITE_POLICY_LINE, diagnose_workspace, disk_space_line
from repairlib.lifecycle.backup_archive import (
    create_archive, restore_archive, verify_backup, verify_restore_point,
)
from repairlib.lifecycle.backup_schedule import backup_line, backup_state, scheduled_snapshot
from repairlib.lifecycle.checkout import CheckoutError
from repairlib.lifecycle.operations import cleanup_workspace, local_status, reset_demo_workspace
from repairlib.lifecycle.profile import profile_check, profile_lines
from repairlib.lifecycle.recovery import snapshot_workspace
from repairlib.lifecycle.support import write_bundle
from repairlib.lifecycle.workspace import (
    SETUP_STATES, WorkspaceError, migrate_workspace, workspace_status,
)


def local_timezone() -> str | None:
    """Return this machine's IANA zone from TZ or /etc/localtime, if any."""
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    link = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in link:
        candidates.append(link.split("/zoneinfo/", 1)[1])
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate:
            try:
                ZoneInfo(candidate)
            except (ValueError, ZoneInfoNotFoundError):
                continue
            return candidate
    return None


def guarded(label, os_message: str, *, refusals: tuple = (), password: bool = False):
    """Report a refusal as `label: message` with exit 1, and any OS failure as a fixed message.

    `label` is a string or a function of the parsed args. Messages never include paths or secrets.
    """
    def decorate(handler):
        hidden = (EOFError, getpass.GetPassWarning) if password else ()

        def run(args) -> int:
            name = label(args) if callable(label) else label
            try:
                return handler(args)
            except hidden:
                print(f"{name}: could not read a hidden password.", file=sys.stderr)
                return 1
            except (WorkspaceError, *refusals) as error:
                print(f"{name}: {error}", file=sys.stderr)
                return 1
            except OSError:
                print(f"{name}: {os_message}", file=sys.stderr)
                return 1
        return run
    return decorate


def register_workspace_commands(commands) -> None:
    """Parsers for the commands below, in the order `repairlib --help` lists them."""
    dev = commands.add_parser("dev", help="run the loopback development server")
    dev.add_argument("--workspace", default=None)
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)
    dev.add_argument("--browser-setup", action="store_true", help="print a one-time setup link")

    service = commands.add_parser("service", help="manage a Linux systemd user or OpenRC system service, or run in foreground")
    service.add_argument("action", choices=("install", "status", "start", "stop", "restart", "remove", "foreground"))
    # Optional so `bin/repairlib service install` needs no paths: the launcher exports REPAIRLIB_WORKSPACE.
    add_workspace(service)
    service.add_argument("--port", type=int, default=8000)
    service.add_argument("--openrc-system", action="store_true",
                         help="manage a root-supervised OpenRC system service that runs as this user")

    links = commands.add_parser("links", help="link the repairlib command and the agent skill into this account")
    links.add_argument("--executable", type=Path, default=None,
                       help="launcher to link instead of this tree's (default: bin/repairlib)")

    doctor = commands.add_parser("doctor", help="read-only workspace diagnosis")
    doctor.add_argument("--workspace", default=None)

    status = commands.add_parser("status", help="report local workspace readiness and record counts")
    status.add_argument("--workspace", default=None)
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")

    update = commands.add_parser("update", help="back up, rehearse and update this checkout and its service")
    update.add_argument("--check", action="store_true",
                        help="report the current and available commit without changing anything")

    profile = commands.add_parser("profile", help="report the facts an agent needs before writing")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    check = profile_commands.add_parser("check", help="read-only pre-write check")
    add_workspace(check)
    check.add_argument("--json", action="store_true", help="print machine-readable JSON")

    cleanup = commands.add_parser(
        "cleanup", help="remove stale uploads, expired sessions and old request receipts while stopped")
    cleanup.add_argument("--workspace", default=None)

    backup = commands.add_parser("backup", help="create, verify and restore offline workspace backups")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    snapshot = backup_commands.add_parser(
        "snapshot", help="take a database-only snapshot while stopped, or of a running app with --online")
    snapshot.add_argument("--workspace", default=None)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--online", action="store_true",
                          help="copy a running workspace through SQLite's backup API")
    schedule = backup_commands.add_parser(
        "schedule", help="take the daily online snapshot when it is due")
    schedule.add_argument("--workspace", default=None)
    archive = backup_commands.add_parser("create", help="archive the stopped workspace and retained files")
    archive.add_argument("--workspace", default=None)
    archive.add_argument("--output", type=Path, required=True)
    verify = backup_commands.add_parser("verify", help="verify an archive or standalone database snapshot")
    verify.add_argument("artifact", type=Path)
    restore = backup_commands.add_parser("restore", help="restore a verified archive into a new workspace")
    restore.add_argument("artifact", type=Path)
    restore.add_argument("--workspace", type=Path, required=True)

    support = commands.add_parser("support", help="collect read-only system facts for a support request")
    support_commands = support.add_subparsers(dest="support_command", required=True)
    bundle = support_commands.add_parser("bundle", help="write a zip of system facts and counts only")
    bundle.add_argument("--workspace", default=None)
    bundle.add_argument("--output", type=Path, required=True)

    setup = commands.add_parser("setup", help="initialize a workspace and first admin")
    setup.add_argument("--workspace", default=None)
    setup.add_argument("--shop", required=True)
    setup.add_argument("--timezone", default=None,
                       help="IANA zone; defaults to this machine's zone")
    setup.add_argument("--language", default="", help="default interface language code")
    setup.add_argument("--username", required=True)

    demo = commands.add_parser("demo", help="initialize a new workspace with synthetic customers")
    demo.add_argument("--workspace", default=None)
    demo.add_argument("--username", default="owner")
    demo_commands = demo.add_subparsers(dest="demo_command")
    reset = demo_commands.add_parser("reset", help="reseed an untouched synthetic demo while stopped")
    reset.add_argument("--workspace", default=argparse.SUPPRESS)
    reset.add_argument("--confirm", action="store_true", required=True)

    migrate = commands.add_parser("migrate", help="upgrade an initialized workspace explicitly")
    migrate.add_argument("--workspace", default=None)
    migrate.add_argument("--backup", type=Path, default=None,
                         help="verified backup of the current schema required before upgrading")
    reconcile = commands.add_parser(
        "reconcile-files",
        help="while stopped, remove never-committed upload files and verify attachment bytes",
    )
    reconcile.add_argument("--workspace", default=None)
    reconcile.add_argument("--json", action="store_true", help="print machine-readable JSON")
    mcp = commands.add_parser("mcp", help="serve workspace tools over MCP stdio as the local 'mcp' user")
    add_workspace(mcp)
    dashboard = commands.add_parser("dashboard", help="show open work, pickup and low-stock counts")
    add_workspace(dashboard)
    dashboard.add_argument("--json", action="store_true", help="print machine-readable JSON")
    catalog = commands.add_parser("catalog", help="list every CLI command and MCP tool with its arguments")
    catalog.add_argument("--json", action="store_true", help="print machine-readable JSON")


def print_update_state(state: dict) -> None:
    print(f"Update phase: {state['update_phase']} (writes: {'open' if state['writes_open'] else 'closed'})")


def run_status(args, update_state: dict | None) -> int:
    workspace = resolve_workspace(args.workspace)
    report = {**local_status(workspace,
                             writes_closed=update_state is not None and not update_state["writes_open"]),
              "backup": backup_state(workspace)}
    if args.json:
        print(json.dumps({**report, **(update_state or {}), "sqlite": SQLITE_POLICY}))
    else:
        print(f"Workspace: {report['workspace']}")
        print(f"State: {report['status']} (ready: {'yes' if report['ready'] else 'no'})")
        if update_state is not None:
            print_update_state(update_state)
        print(f"Schema: {report['schema_version'] or 'unavailable'} "
              f"(expected {report['expected_schema_version']})")
        print(SQLITE_POLICY_LINE)
        print(disk_space_line(report["disk_free_bytes"]))
        print(backup_line(report["backup"]))
        if report["ready"]:
            print(f"Customers: {report['customers']}, repairs: {report['repairs']}")
            print(f"Synthetic demo seeded: {'yes' if report['demo_seeded'] else 'no'}")
    return int(not report["ready"] and report["status"] not in SETUP_STATES)


def run_doctor(args, update_state: dict | None) -> int:
    report = diagnose_workspace(args.workspace)
    print("\n".join((*report.lines(), SQLITE_POLICY_LINE)))
    if update_state is not None:
        print_update_state(update_state)
    return report.exit_status


def run_links(args) -> int:
    from repairlib.lifecycle.links import APP_ROOT, LinkError, default_executable, install_links

    try:
        links = install_links(args.executable or default_executable(), APP_ROOT / "skills" / "repairlib",
                              Path.home())
    except LinkError as error:
        print(f"links: {error}", file=sys.stderr)
        return 1
    blocked = [link for link in links.values() if link["status"] == "blocked"]
    for link in links.values():
        print(f"{link['path']} -> {link['target']} ({link['status']})")
    for link in blocked:
        print(f"links: {link['path']} exists and is not a symlink; move it and run this again.",
              file=sys.stderr)
    return 1 if blocked else 0


def run_profile(args) -> int:
    report = profile_check(resolve_workspace(args.workspace))
    print(json.dumps(report) if args.json else "\n".join(profile_lines(report)))
    for blocker in report["blockers"]:
        print(f"profile check: {blocker}", file=sys.stderr)
    return int(bool(report["blockers"]))


def run_support(args) -> int:
    try:
        written = write_bundle(resolve_workspace(args.workspace), args.output.expanduser())
    except FileExistsError as error:
        print(f"support: {error}", file=sys.stderr)
        return 1
    print(f"Support bundle written: {written}")
    return 0


@guarded(lambda args: f"backup {args.backup_command}", "could not safely access backup or workspace files.")
def run_backup(args) -> int:
    if args.backup_command == "snapshot":
        version = snapshot_workspace(resolve_workspace(args.workspace), args.output.expanduser(),
                                     online=args.online)
        print(f"Verified SQLite snapshot saved (schema {version}).")
    elif args.backup_command == "schedule":
        taken = scheduled_snapshot(resolve_workspace(args.workspace))
        print(f"Verified online snapshot saved: {taken}" if taken
              else "A verified snapshot is already current.")
    elif args.backup_command == "create":
        version = create_archive(resolve_workspace(args.workspace), args.output.expanduser())
        print(f"Backup archive saved (schema {version}).")
    elif args.backup_command == "restore":
        version = restore_archive(args.artifact.expanduser(), args.workspace.expanduser())
        print(f"Backup archive restored (schema {version}).")
    else:
        kind, version = verify_backup(args.artifact.expanduser())
        print(f"{kind} verified (schema {version}).")
    return 0


@guarded(lambda args: args.command, "could not safely access workspace state.")
def run_maintenance(args) -> int:
    """`cleanup` and `demo reset`."""
    workspace = resolve_workspace(args.workspace)
    if args.command == "cleanup":
        report = cleanup_workspace(workspace)
        print(f"Removed {report['staging']} stale upload staging files, "
              f"{report['sessions']} expired sessions and {report['receipts']} old request receipts.")
    else:
        reset_demo_workspace(workspace)
        print("Synthetic demo customers reseeded. Accounts and credentials preserved.")
    return 0


@guarded("reconcile-files", "could not access workspace state.")
def run_reconcile_files(args) -> int:
    from repairlib.domain.attachments import reconcile_files

    report = reconcile_files(resolve_workspace(args.workspace))
    damaged = bool(report["missing"] or report["mismatched"])
    if args.json:
        print(json.dumps(report))
    else:
        print(f"Checked {report['checked']} attachments; removed {report['removed_staging']} "
              f"staging and {report['removed_orphans']} uncommitted files.")
        for name in ("missing", "mismatched"):
            if report[name]:
                print(f"{name.title()} attachment IDs: {', '.join(map(str, report[name]))}")
    if damaged:
        print("reconcile-files: restore damaged attachment bytes from a verified backup.",
              file=sys.stderr)
    return 2 if damaged else 0


def run_dev(args) -> int:
    import secrets

    import uvicorn

    from repairlib.app import create_app

    if args.host not in LOOPBACK_BIND_HOSTS:
        print("dev: choose a loopback host (127.0.0.1, localhost or ::1).", file=sys.stderr)
        return 1
    workspace = resolve_workspace(args.workspace)
    token = None
    if args.browser_setup:
        if not sys.stdout.isatty() or not 1 <= args.port <= 65535:
            print("dev: browser setup needs an interactive terminal and a fixed port.",
                  file=sys.stderr)
            return 1
        if workspace_status(workspace) not in SETUP_STATES:
            print("dev: workspace is unavailable for browser setup. Run repairlib doctor.",
                  file=sys.stderr)
            return 1
        token = secrets.token_urlsafe(32)
        host = f"[{args.host}]" if ":" in args.host else args.host
        print(f"Open the one-time setup link: http://{host}:{args.port}/setup/exchange?token={token}",
              flush=True)
    uvicorn.run(
        create_app(workspace=workspace, bootstrap_token=token),
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        ws="none",
    )
    return 0


@guarded("migrate", "could not access workspace state.", refusals=(ValueError,))
def run_migrate(args) -> int:
    check = None if args.backup is None else (
        lambda schema: verify_restore_point(args.backup.expanduser(), schema))
    verified = migrate_workspace(resolve_workspace(args.workspace), check_backup=check)
    if verified:
        print(verified)
    print("Workspace schema is current.")
    return 0


@guarded("demo", "could not access or publish workspace state.", refusals=(ValueError,), password=True)
def run_demo(args) -> int:
    from repairlib.domain.setup import initialize_demo_workspace

    password = read_password("Demo admin password: ")
    initialize_demo_workspace(resolve_workspace(args.workspace), username=args.username, password=password)
    print("Synthetic demo workspace initialized. Start the development server to sign in.")
    return 0


@guarded("update", "could not update this checkout.", refusals=(ValueError, CheckoutError))
def run_update(args) -> int:
    from repairlib.lifecycle.checkout import current_commit, remote_commit
    from repairlib.lifecycle.service import SOURCE

    repo = SOURCE.parent
    if not (repo / ".git").exists():
        raise CheckoutError("update applies to a Git checkout; an installed launcher uses the staged update recipe.")
    if args.check:
        now, available = current_commit(repo), remote_commit(repo)
        print(f"checkout {now} · origin {available}")
        return 0
    from repairlib.lifecycle.checkout_update import update_checkout

    old, new, service, backup = update_checkout(
        repo, progress=lambda stage: print(f"Update: {stage}…", file=sys.stderr, flush=True),
    )
    if old == new:
        print(f"Already up to date at {old[:7]}.")
        return 0
    print(f"Updated {old[:7]} → {new[:7]}; service {service}.")
    print(f"Verified full backup: {backup}")
    return 0


@guarded("setup", "could not access or publish workspace state.", refusals=(ValueError,), password=True)
def run_setup(args) -> int:
    from repairlib.domain.setup import initialize_workspace

    password = read_password("First admin password: ")
    timezone = args.timezone or local_timezone()
    if not timezone:
        raise ValueError("pass --timezone: this machine does not expose an IANA zone.")
    initialize_workspace(
        resolve_workspace(args.workspace), shop=args.shop, timezone=timezone,
        username=args.username, password=password, language=args.language,
    )
    print("Workspace initialized. Restart the development server to read the new state.")
    return 0
