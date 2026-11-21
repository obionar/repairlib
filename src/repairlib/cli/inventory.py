"""Inventory catalog commands run against the local workspace."""

import argparse
from dataclasses import asdict
import json
import sys

from repairlib import money
from repairlib.cli.common import add_revision, add_workspace, display_name
from repairlib.client import local

TEXT = ("sku", "description", "category", "manufacturer", "part_number", "package",
        "specifications", "bin", "unit")
NUMBERS = {"low_stock": "low_stock_milli", "purchase_cost": "purchase_cost_cents",
           "selling_price": "selling_price_cents"}


def parser_for(parse):
    def convert(value: str) -> int:
        try:
            return parse(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from None
    return convert


def signed_milli(value: str) -> int:
    negative = value.startswith("-")
    try:
        milli = money.parse_milli(value[1:] if negative else value.removeprefix("+"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return -milli if negative else milli


PARSERS = {"low_stock": parser_for(money.parse_threshold), "purchase_cost": parser_for(money.parse_cents),
           "selling_price": parser_for(money.parse_cents)}
HELP = {"low_stock": "decimal quantity, up to 3 places", "purchase_cost": "EUR, up to 2 decimal places",
        "selling_price": "EUR, up to 2 decimal places"}


def register_inventory_commands(commands) -> None:
    inventory = commands.add_parser("inventory", help="manage the shop inventory catalog")
    actions = inventory.add_subparsers(dest="inventory_action", required=True)
    listing = actions.add_parser("list", help="list a page of catalog items")
    listing.add_argument("--include-archived", action="store_true")
    listing.add_argument("--low-stock", action="store_true",
                         help="list active items at or below their stock threshold")
    listing.add_argument("--limit", default=25, type=int)
    listing.add_argument("--before", type=int, help="cursor from the preceding page")
    listing.add_argument("--query", default="", help="search SKU, description, manufacturer or part number")
    listing.add_argument("--category", default="", help="match a category")
    create = actions.add_parser("create", help="create one catalog item")
    show = actions.add_parser("show", help="show a catalog item and its current revision")
    edit = actions.add_parser("edit", help="edit reviewed catalog item fields")
    archive = actions.add_parser("archive", help="archive a reviewed catalog item")
    restore = actions.add_parser("restore", help="restore a reviewed archived catalog item")
    for command in (show, edit, archive, restore):
        command.add_argument("id", type=int)
    for command in (create, edit, archive, restore):
        command.add_argument("--key", required=True, help="unique request key reused on retry")
    for command in (edit, archive, restore):
        add_revision(command)
    for field in TEXT:
        option = "--" + field.replace("_", "-")
        if field in {"sku", "description"}:
            create.add_argument(option, required=True)
        else:
            create.add_argument(option, default="pcs" if field == "unit" else "")
        edit.add_argument(option)
    for field, parse in PARSERS.items():
        option = "--" + field.replace("_", "-")
        create.add_argument(option, type=parse, help=HELP[field])
        edit.add_argument(option, type=parse, help=HELP[field])
    edit.add_argument("--clear-purchase-cost", action="store_true", help="remove the default purchase cost")
    receive = actions.add_parser("receive", help="receive stock into a reviewed item")
    adjust = actions.add_parser("adjust", help="adjust stock by a signed quantity with a reason")
    reverse = actions.add_parser("reverse", help="reverse one stock movement with a compensating entry")
    movements = actions.add_parser("movements", help="list a page of an item's stock movements")
    reconcile = actions.add_parser("reconcile", help="check stock balances and repair links")
    for command in (receive, adjust, reverse, movements):
        command.add_argument("id", type=int)
    reverse.add_argument("movement", type=int)
    receive.add_argument("--quantity", required=True, type=parser_for(money.parse_milli),
                         help="decimal quantity, up to 3 places")
    adjust.add_argument("--quantity", required=True, type=signed_milli,
                        help="signed decimal quantity, e.g. -1.5 to remove")
    receive.add_argument("--reason", default="", help="optional note such as a delivery reference")
    for command in (adjust, reverse):
        command.add_argument("--reason", required=True)
    for command in (receive, adjust, reverse):
        add_revision(command)
        command.add_argument("--key", required=True, help="unique request key reused on retry")
    movements.add_argument("--limit", default=25, type=int)
    movements.add_argument("--before", type=int, help="cursor from the preceding page")
    for command in (listing, create, show, edit, archive, restore, receive, adjust, reverse, movements, reconcile):
        add_workspace(command)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def signed(milli: int) -> str:
    return ("-" if milli < 0 else "") + money.format_milli(abs(milli))


def item_fields(item) -> dict:
    return {name: getattr(item, name) for name in (*TEXT, *NUMBERS.values())}


def run_inventory_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        action = args.inventory_action
        if action == "list":
            page = local.list_inventory(ctx, limit=args.limit, before=args.before,
                                       include_archived=args.include_archived, low_stock=args.low_stock,
                                       query=args.query, category=args.category)
            if args.json:
                print(json.dumps(asdict(page)))
            else:
                for item in page.items:
                    status = "\tArchived" if item.archived_at else ""
                    print(f"{item.id}\t{display_name(item.sku)}\t{display_name(item.description)}\t"
                          f"{money.format_cents(item.selling_price_cents)} EUR{status}")
                if page.next_before is not None:
                    print(f"Next page: --before {page.next_before}", file=sys.stderr)
        elif action == "create":
            details = {name: getattr(args, name) for name in TEXT}
            for name, target in NUMBERS.items():
                value = getattr(args, name)
                if value is not None or name == "purchase_cost":
                    details[target] = value
            item = local.create_inventory_item(ctx, details, args.key)
            print(json.dumps(asdict(item)) if args.json else f"Created inventory item {item.id}.")
        elif action == "show":
            item = local.show_inventory_item(ctx, args.id)
            if args.json:
                print(json.dumps(asdict(item)))
            else:
                print(f"{item.id}\t{display_name(item.sku)}\tRevision {item.revision}")
                if item.archived_at:
                    print(f"Archived\t{item.archived_at}")
                for name in TEXT[1:]:
                    if getattr(item, name):
                        print(f"{name.replace('_', ' ').title()}\t{display_name(getattr(item, name))}")
                print(f"On hand\t{money.format_milli(item.on_hand_milli)} {display_name(item.unit)}")
                print(f"Low stock\t{money.format_milli(item.low_stock_milli)} {display_name(item.unit)}")
                if item.purchase_cost_cents is not None:
                    print(f"Purchase cost\t{money.format_cents(item.purchase_cost_cents)} EUR")
                print(f"Selling price\t{money.format_cents(item.selling_price_cents)} EUR")
        elif action == "edit":
            details = item_fields(local.show_inventory_item(ctx, args.id))
            details.update({name: value for name in TEXT if (value := getattr(args, name)) is not None})
            details.update({target: value for name, target in NUMBERS.items()
                            if (value := getattr(args, name)) is not None})
            if args.clear_purchase_cost:
                details["purchase_cost_cents"] = None
            details["revision"] = args.revision
            item = local.edit_inventory_item(ctx, args.id, details, args.key)
            print(json.dumps(asdict(item)) if args.json else
                  f"Saved inventory item {item.id} at revision {item.revision}.")
        elif action in ("archive", "restore"):
            change = local.archive_inventory_item if action == "archive" else local.restore_inventory_item
            item = change(ctx, args.id, revision=args.revision, key=args.key)
            print(json.dumps(asdict(item)) if args.json else
                  f"{action.capitalize()}d inventory item {item.id} at revision {item.revision}.")
        elif action in ("receive", "adjust", "reverse"):
            body = {"reason": args.reason, "revision": args.revision}
            if action != "reverse":
                body["quantity_milli"] = args.quantity
            path = f"movements/{args.movement}/reverse" if action == "reverse" else action
            change = local.change_stock(ctx, args.id, path, body, args.key)
            print(json.dumps(asdict(change)) if args.json else
                  f"Recorded stock movement {change.movement.id}; on hand "
                  f"{signed(change.item.on_hand_milli)} at revision {change.item.revision}.")
        elif action == "movements":
            page = local.list_movements(ctx, args.id, limit=args.limit,
                                       before=args.before)
            if args.json:
                print(json.dumps(asdict(page)))
            else:
                for movement in page.movements:
                    print(f"{movement.id}\t{movement.kind}\t{signed(movement.quantity_milli)}\t"
                          f"{signed(movement.on_hand_after_milli)}\t{display_name(movement.actor_username)}\t"
                          f"{movement.created_at}\t{display_name(movement.reason)}")
                if page.next_before is not None:
                    print(f"Next page: --before {page.next_before}", file=sys.stderr)
        elif action == "reconcile":
            result = local.reconcile_stock(ctx)
            if args.json:
                print(json.dumps(asdict(result)))
            else:
                print(f"{result.items_checked} items checked, {len(result.mismatches)} mismatches, "
                      f"{len(result.issues)} ledger issues")
                for mismatch in result.mismatches:
                    print(f"Mismatch\t{mismatch.item_id}\t{display_name(mismatch.sku)}\t"
                          f"cached {signed(mismatch.cached_milli)}\tledger {signed(mismatch.ledger_milli)}")
                for issue in result.issues:
                    detail = f"movement {issue.movement_id}"
                    if issue.item_id is not None:
                        detail += f", item {issue.item_id}"
                    if issue.charge_id is not None:
                        detail += f", charge {issue.charge_id}"
                    print(f"{issue.code}\t{detail}")
            return int(bool(result.mismatches or result.issues))
    except (OSError, ValueError):
        print("inventory: Could not read the workspace.", file=sys.stderr)
        return 1
    return 0
