"""Customer commands run against the local workspace."""

from dataclasses import asdict
import json
import sys

from repairlib.cli.common import add_revision, add_workspace, display_name
from repairlib.client import local
from repairlib.contracts import CustomerWithNotices

FIELDS = ("name", "phone", "email", "address", "notes")


def print_notices(customer):
    if isinstance(customer, CustomerWithNotices):
        for warning in customer.contact_warnings.values():
            print(warning, file=sys.stderr)
        if customer.possible_duplicate_id is not None:
            print(f"Possible duplicate customer {customer.possible_duplicate_id}.", file=sys.stderr)


def register_customer_commands(commands) -> None:
    customers = commands.add_parser("customers", help="manage customers and read their history")
    actions = customers.add_subparsers(dest="customer_action", required=True)
    listing = actions.add_parser("list", help="list a page of customers")
    listing.add_argument("--include-archived", action="store_true")
    history = actions.add_parser("history", help="read attributed customer changes")
    for command in (listing, history):
        command.add_argument("--limit", default=25, type=int)
        command.add_argument("--before", type=int, help="cursor from the preceding page")
    listing.add_argument("--query", default="", help="case- and accent-insensitive name/phone/email search")
    create = actions.add_parser("create", help="create one customer")
    create.add_argument("--key", required=True, help="unique request key reused on retry")
    create.add_argument("--name", required=True)
    for field in FIELDS[1:]:
        create.add_argument(f"--{field}", default="")
    show = actions.add_parser("show", help="show a customer and their current revision")
    edit = actions.add_parser("edit", help="edit reviewed customer fields")
    archive = actions.add_parser("archive", help="archive a reviewed customer")
    restore = actions.add_parser("restore", help="restore a reviewed customer")
    anonymize = actions.add_parser("anonymize", help="irreversibly erase a customer's personal data")
    export = actions.add_parser("export", help="print one customer's record, repairs and charges as JSON")
    for command in (show, edit, archive, restore, history, anonymize, export):
        command.add_argument("id", type=int)
    for command in (edit, archive, restore, anonymize):
        add_revision(command)
        command.add_argument("--key", required=True, help="unique request key reused on retry")
    for field in FIELDS:
        edit.add_argument(f"--{field}")
    for command in (listing, create, show, edit, archive, restore, history, anonymize, export):
        add_workspace(command)
    for command in (listing, create, show, edit, archive, restore, history, anonymize):
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def run_customer_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.customer_action == "list":
            page = local.list_customers(ctx, limit=args.limit, before=args.before,
                                       query=args.query, include_archived=args.include_archived)
            if args.json:
                print(json.dumps(asdict(page)))
            else:
                for customer in page.items:
                    status = "\tArchived" if customer.archived_at else ""
                    print(f"{customer.id}\t{display_name(customer.name)}{status}")
                if page.next_before is not None:
                    print(f"Next page: --before {page.next_before}", file=sys.stderr)
        elif args.customer_action == "create":
            details = {name: getattr(args, name) for name in FIELDS}
            customer = local.create_customer(ctx, details, args.key)
            print(json.dumps(asdict(customer)) if args.json else f"Created customer {customer.id}.")
            if not args.json:
                print_notices(customer)
        elif args.customer_action == "show":
            customer = local.show_customer(ctx, args.id)
            if args.json:
                print(json.dumps(asdict(customer)))
            else:
                print(f"{customer.id}\t{display_name(customer.name)}\tRevision {customer.revision}")
                if customer.archived_at:
                    print(f"Archived\t{customer.archived_at}")
                for name in FIELDS[1:]:
                    value = getattr(customer, name)
                    if value:
                        print(f"{name.title()}\t{display_name(value)}")
                print_notices(customer)
        elif args.customer_action == "edit":
            details = {name: value for name in FIELDS if (value := getattr(args, name)) is not None}
            details["revision"] = args.revision
            customer = local.edit_customer(ctx, args.id, details, args.key)
            print(json.dumps(asdict(customer)) if args.json else
                  f"Saved customer {customer.id} at revision {customer.revision}.")
            if not args.json:
                print_notices(customer)
        elif args.customer_action in {"archive", "restore"}:
            enabled = args.customer_action == "archive"
            customer = local.change_customer_archive(ctx, args.id,
                                                    revision=args.revision, enabled=enabled, key=args.key)
            verb = "Archived" if enabled else "Restored"
            print(json.dumps(asdict(customer)) if args.json else
                  f"{verb} customer {customer.id} at revision {customer.revision}.")
            if not args.json:
                print_notices(customer)
        elif args.customer_action == "anonymize":
            customer = local.anonymize_customer(ctx, args.id, revision=args.revision, key=args.key)
            print(json.dumps(asdict(customer)) if args.json else
                  f"Anonymized customer {customer.id} at revision {customer.revision}.")
        elif args.customer_action == "export":
            print(json.dumps(asdict(local.export_customer(ctx, args.id)), ensure_ascii=False))
        elif args.customer_action == "history":
            page = local.customer_history(ctx, args.id, limit=args.limit,
                                         before=args.before)
            if args.json:
                print(json.dumps(asdict(page)))
            else:
                for event in page.items:
                    source = event.source
                    if event.actor_kind == "agent":
                        source += " agent"
                    if event.token_label:
                        source += f" ({display_name(event.token_label)})"
                    print(f"{event.occurred_at}\t{event.operation}\t{display_name(event.actor_username)}\t{source}")
                    print(f"  {json.dumps(event.changes)}")
                if page.next_before is not None:
                    print(f"Next page: --before {page.next_before}", file=sys.stderr)
    except (OSError, ValueError):
        print("customers: Could not read the workspace.", file=sys.stderr)
        return 1
    return 0
