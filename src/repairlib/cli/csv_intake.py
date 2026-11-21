"""Customer, catalog and opening-stock CSV commands run against the local workspace."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys

from repairlib.cli.common import add_workspace
from repairlib.client import local
from repairlib.contracts import CSV_MAX_BYTES
from repairlib.lifecycle.workspace import WorkspaceError


KINDS = {
    "customers": ("create customers from CSV", ("Created {} customers.", "name")),
    "catalog": ("create inventory items with optional opening stock", ("Created {} inventory items.", "sku")),
    "opening-stock": ("record opening stock for existing items", ("Received opening stock for {} items.", "sku")),
}


def register_csv_commands(commands) -> None:
    csv = commands.add_parser("csv", help="preview and apply a CSV intake batch")
    kinds = csv.add_subparsers(dest="csv_kind", required=True)
    for kind, (summary, _) in KINDS.items():
        register_kind(kinds.add_parser(kind, help=summary))


def register_kind(parser) -> None:
    actions = parser.add_subparsers(dest="csv_action", required=True)
    template = actions.add_parser("template", help="write the CSV header to stdout")
    preview = actions.add_parser("preview", help="review proposed rows and errors")
    apply = actions.add_parser("apply", help="apply the reviewed CSV")
    apply.add_argument("--preview-id", required=True, help="ID from a clean preview")
    apply.add_argument("--sha256", required=True, help="input SHA-256 from that preview")
    apply.add_argument("--key", required=True, help="unique request key reused on retry")
    for command in (preview, apply):
        command.add_argument("file", type=Path)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")
    for command in (template, preview, apply):
        add_workspace(command)


def read_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise local.ClientError("Choose a regular CSV file.", 8)
        raw = handle.read(CSV_MAX_BYTES + 1)
    if len(raw) > CSV_MAX_BYTES:
        raise local.ClientError("The CSV exceeds its size limit.", 8)
    return raw


def run_csv_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.csv_action == "template":
            sys.stdout.buffer.write(local.csv_template(ctx, args.csv_kind))
            return 0
        raw = read_file(args.file.expanduser())
        summary, label = KINDS[args.csv_kind][1]
        if args.csv_action == "apply":
            result = local.apply_csv(ctx, args.csv_kind, raw,
                                    preview_id=args.preview_id, input_hash=args.sha256, key=args.key)
            lines = [summary.format(len(result.rows))]
        else:
            result = local.preview_csv(ctx, args.csv_kind, raw)
            lines = [
                f"SHA-256\t{result.sha256}",
                f"Preview ID\t{result.preview_id or 'Requires correction'}",
                *(f"Row {row.row}\t{row.action}\t{row.fields[label]}" for row in result.actions),
                *(f"Row {issue.row}\t{issue.field}\t{issue.message}" for issue in result.errors),
            ]
        print(json.dumps(asdict(result), ensure_ascii=False) if args.json else "\n".join(lines))
        # A preview without an id has blocking errors; scripts that only read $? must see a refusal.
        return 0 if args.csv_action == "apply" or result.preview_id else 7
    except WorkspaceError:
        print("csv: The workspace is not available.", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("csv: Could not read the CSV file.", file=sys.stderr)
        return 8
