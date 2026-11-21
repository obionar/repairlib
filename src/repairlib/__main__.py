"""Argparse entry point for the dev server and local workspace operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import uuid4

from repairlib.client.local import ClientError
from repairlib.cli.customers import register_customer_commands, run_customer_command
from repairlib.cli.csv_intake import register_csv_commands, run_csv_command
from repairlib.cli import dry_run
from repairlib.cli.catalog import run_catalog_command
from repairlib.cli.dashboard import run_dashboard_command
from repairlib.cli.inventory import register_inventory_commands, run_inventory_command
from repairlib.cli.repairs import register_repair_commands, run_repair_command
from repairlib.cli.portable_shop import register_portable_commands, run_portable_command
from repairlib.cli.printer import register_printer_commands, run_printer_command
from repairlib.cli.settings import register_settings_commands, run_settings_command
from repairlib.cli.users import register_user_commands, run_passwd_command, run_user_command
from repairlib.cli.workspace import (
    register_workspace_commands, run_backup, run_demo, run_dev, run_doctor, run_links, run_maintenance,
    run_migrate, run_profile, run_reconcile_files, run_setup, run_status, run_support, run_update,
)
from repairlib.lifecycle.checkout import version_text
from repairlib.config import resolve_workspace

# Commands that call shared domain operations as the local `cli` user: (runner, supports --dry-run).
CLIENT_COMMANDS = {
    "csv": (run_csv_command, False),
    "customers": (run_customer_command, True),
    "inventory": (run_inventory_command, False),
    "repairs": (run_repair_command, True),
    "users": (run_user_command, False),
    "printer": (run_printer_command, False),
    "portable": (run_portable_command, False),
    "settings": (run_settings_command, False),
    "dashboard": (run_dashboard_command, False),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repairlib")
    parser.add_argument("--version", action="version", version=version_text())
    parser.add_argument("--json", action="store_true", dest="global_json",
                        help="print machine-readable JSON for commands and errors")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="validate a mutation and report what it would change without writing")
    commands = parser.add_subparsers(dest="command", required=True)
    register_workspace_commands(commands)
    register_customer_commands(commands)
    register_csv_commands(commands)
    register_inventory_commands(commands)
    register_repair_commands(commands)
    register_user_commands(commands)
    register_settings_commands(commands)
    register_portable_commands(commands)
    register_printer_commands(commands)
    return parser


def run_command(args, prefix: str, operation) -> int:
    """Run a CLI command and render its ClientError at the dispatch boundary."""
    try:
        return operation(args)
    except ClientError as error:
        if getattr(args, "json", False):
            print(json.dumps({"error": {"code": error.code, "message": str(error), "fields": {}}},
                             ensure_ascii=False))
        else:
            print(f"{prefix}: {error}", file=sys.stderr)
        return error.exit_code


def with_dry_run(args, operation):
    return (lambda _: dry_run.run(args, operation)) if args.dry_run else operation


def run_service_command(args) -> int:
    from repairlib.lifecycle.service import run_service

    return run_service(args.action, resolve_workspace(args.workspace), args.port,
                       openrc_system=args.openrc_system)


def run_mcp_command(args) -> int:
    from repairlib.adapters.mcp import run_mcp

    return run_command(args, "mcp", lambda _: run_mcp(args.workspace))


def main(argv: Sequence[str] | None = None, *, update_state: dict | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.global_json:
        args.json = True
    # `--key auto` mints a fresh idempotency key so scripts need not invent one.
    if getattr(args, "key", None) == "auto":
        args.key = uuid4().hex
    if args.dry_run:
        def refuse(_):
            dry_run.check(args)
            return None

        refused = run_command(args, args.command, refuse)
        if refused is not None:
            return refused
    command = args.command
    if command in CLIENT_COMMANDS:
        runner, supports_dry_run = CLIENT_COMMANDS[command]
        return run_command(args, command, with_dry_run(args, runner) if supports_dry_run else runner)
    if command in {"passwd", "recover-password"}:
        return run_command(args, "passwd", run_passwd_command)
    if command == "catalog":
        return run_command(args, "catalog", lambda _: run_catalog_command(args, build_parser()))
    if command == "demo":
        return (run_maintenance if args.demo_command == "reset" else run_demo)(args)
    handlers = {
        "service": run_service_command, "mcp": run_mcp_command, "support": run_support, "links": run_links,
        "backup": run_backup, "profile": run_profile, "cleanup": run_maintenance,
        "reconcile-files": run_reconcile_files, "dev": run_dev, "migrate": run_migrate,
        "setup": run_setup, "update": run_update,
        "status": lambda a: run_status(a, update_state), "doctor": lambda a: run_doctor(a, update_state),
    }
    return handlers[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
