"""Dashboard summary for the local workspace."""

from dataclasses import asdict
import json
import sys

from repairlib.cli.common import display_name
from repairlib.client import local


def run_dashboard_command(args) -> int:
    try:
        board = local.read_dashboard(local.open_workspace(args.workspace))
    except OSError:
        print("dashboard: Could not read the workspace.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(board), ensure_ascii=False))
        return 0
    print(f"Open work\t{board.open_count}\nAwaiting pickup\t{board.pickup_count}\nLow stock\t{board.low_stock_count}")
    for repair in board.open_work:
        print(f"open\t{repair.id}\t{display_name(repair.model)}\t{repair.stage}")
    for repair in board.awaiting_pickup:
        print(f"pickup\t{repair.id}\t{display_name(repair.model)}\t{repair.stage}")
    return 0
