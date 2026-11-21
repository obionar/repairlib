"""Administrator printer controls run against the local workspace."""

from dataclasses import asdict
import json
import sys

from repairlib.cli.common import add_revision, add_workspace
from repairlib.client import local


def register_printer_commands(commands) -> None:
    printer = commands.add_parser("printer", help="configure the shop label printer")
    actions = printer.add_subparsers(dest="printer_action", required=True)
    show = actions.add_parser("show", help="show the printer choice and executable availability")
    configure = actions.add_parser("set", help="select P15 or disable direct printing")
    configure.add_argument("driver", choices=("p15", "none"))
    test = actions.add_parser("test", help="send one test label to P15")
    for command in (configure, test):
        add_revision(command)
        command.add_argument("--key", required=True, help="reuse the key and revision to retrieve the result")
    for command in (show, configure, test):
        add_workspace(command)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def label_result(receipt, as_json: bool) -> int:
    acknowledged = receipt.outcome == "acknowledged"
    if as_json:
        print(json.dumps(asdict(receipt)))
    else:
        print(receipt.message, file=sys.stdout if acknowledged else sys.stderr)
    return 0 if acknowledged else 7


def run_printer_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.printer_action == "show":
            result = local.show_printer(ctx)
        elif args.printer_action == "set":
            result = local.configure_printer(ctx, args.driver, args.revision, args.key)
        else:
            receipt = local.send_label(ctx, args.revision, args.key)
            return label_result(receipt, args.json)
        if args.json:
            print(json.dumps(asdict(result)))
        else:
            print(f"Driver\t{result.driver}\nRevision\t{result.revision}\nP15 program available\t{result.available}")
    except (OSError, ValueError):
        print("printer: Could not read the workspace.", file=sys.stderr)
        return 1
    return 0
