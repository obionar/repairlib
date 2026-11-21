"""Repair, work-entry, charge and attachment commands run against the local workspace."""

from argparse import BooleanOptionalAction
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from pydantic import TypeAdapter

from repairlib import money
from repairlib.cli.common import add_revision, add_workspace, display_name
from repairlib.cli.printer import label_result
from repairlib.client import local
from repairlib.contracts import (
    CHARGE_CONSUMPTION_FIELDS, SUMMARY_CONSUMPTION_FIELDS, Attachment, Charge, ChargeSummary, WorkEntry,
)

FIELDS = ("device_type", "brand", "model", "serial", "display_device", "reported_issue", "notes")
KINDS = ("work", "test", "contact", "handover", "note")
CHARGE = TypeAdapter(Charge)
CHARGE_SUMMARY = TypeAdapter(ChargeSummary)
STAGES = ("new", "in_progress", "waiting_parts", "lab_test", "customer_test", "repaired", "no_repair")


def add_common(command):
    add_workspace(command)
    command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def register_repair_commands(commands) -> None:
    repair_commands = commands.add_parser("repairs", help="manage repairs and work entries")
    actions = repair_commands.add_subparsers(dest="repair_action", required=True)
    listing = actions.add_parser("list", help="list a page of repairs")
    listing.add_argument("--limit", default=25, type=int)
    listing.add_argument("--before", type=int)
    listing.add_argument("--query", default="", help="search device, serial, issue or customer name")
    listing.add_argument("--stage", choices=STAGES)
    listing.add_argument("--queue", choices=("open", "pickup"), default="")
    listing.add_argument("--picked-up", action=BooleanOptionalAction)
    listing.add_argument("--archived", action=BooleanOptionalAction)
    listing.add_argument("--customer", type=int)
    listing.add_argument("--technician", default="", help="technician ID, me, or unassigned")
    create = actions.add_parser("create", help="create a repair")
    create.add_argument("--key", required=True)
    create.add_argument("--customer", type=int)
    create.add_argument("--technician", type=int)
    for field in FIELDS:
        create.add_argument(f"--{field.replace('_', '-')}", default="")
    create.add_argument("--warranty-until", help="last covered date in YYYY-MM-DD format")
    create.add_argument("--warranty-repair-id", type=int, help="earlier repair of this shop that this one returns")
    show = actions.add_parser("show", help="show a repair and current revision")
    show.add_argument("id", type=int)
    label = actions.add_parser("label-options", help="show the reviewed label and printer revisions")
    label.add_argument("id", type=int)
    send = actions.add_parser("print-label", help="send one reviewed repair label to P15")
    send.add_argument("id", type=int)
    add_revision(send)
    add_revision(send, "--printer-revision", dest="printer_revision")
    send.add_argument("--key", required=True, help="same key and revisions retrieve the existing outcome")
    edit = actions.add_parser("edit", help="edit a reviewed repair")
    edit.add_argument("id", type=int)
    add_revision(edit)
    edit.add_argument("--key", required=True)
    edit.add_argument("--customer", type=int)
    edit.add_argument("--technician", type=int)
    edit.add_argument("--clear-customer", action="store_true")
    edit.add_argument("--clear-technician", action="store_true")
    for field in FIELDS:
        edit.add_argument(f"--{field.replace('_', '-')}")
    edit.add_argument("--warranty-until", help="last covered date in YYYY-MM-DD format, or empty to clear")
    edit.add_argument("--warranty-repair-id", type=int, help="earlier repair of this shop that this one returns")
    stage = actions.add_parser("stage", help="change a reviewed repair stage")
    stage.add_argument("id", type=int)
    add_revision(stage)
    stage.add_argument("--stage", choices=STAGES, required=True)
    stage.add_argument("--reason", default="", help="required when reopening a terminal repair")
    stage.add_argument("--key", required=True)
    flag_commands = []
    for action in ("pickup", "archive", "payment"):
        flag = actions.add_parser(action, help=f"change repair {action} state")
        flag.add_argument("id", type=int)
        add_revision(flag)
        flag.add_argument("--undo", action="store_true")
        flag.add_argument("--key", required=True)
        flag_commands.append(flag)
    entries = actions.add_parser("entries", help="list attributed work entries")
    activity = actions.add_parser("activity", help="list who changed the repair, newest first")
    for command in (entries, activity):
        command.add_argument("id", type=int)
        command.add_argument("--limit", default=25, type=int)
        command.add_argument("--before", type=int)
    add = actions.add_parser("add-entry", help="append an attributed work entry")
    add.add_argument("id", type=int)
    add.add_argument("--kind", choices=KINDS, default="work")
    add.add_argument("--body", required=True)
    add.add_argument("--key", required=True)
    correct = actions.add_parser("edit-entry", help="correct an entry and preserve its history")
    correct.add_argument("id", type=int)
    correct.add_argument("entry_id", type=int)
    add_revision(correct)
    correct.add_argument("--kind", choices=KINDS, required=True)
    correct.add_argument("--body", required=True)
    correct.add_argument("--reason", required=True)
    correct.add_argument("--key", required=True)
    visibility_commands = []
    for action in ("delete-entry", "restore-entry"):
        visibility = actions.add_parser(action, help="soft-delete or restore an entry with a reason")
        visibility.add_argument("id", type=int)
        visibility.add_argument("entry_id", type=int)
        add_revision(visibility)
        visibility.add_argument("--reason", required=True)
        visibility.add_argument("--key", required=True)
        visibility_commands.append(visibility)
    device = actions.add_parser("device-history", help="list a serial's repairs, oldest first")
    device.add_argument("--serial", required=True)
    history = actions.add_parser("entry-history", help="show immutable entry revisions")
    history.add_argument("id", type=int)
    history.add_argument("entry_id", type=int)
    files = actions.add_parser("attachments", help="list repair attachments")
    files.add_argument("id", type=int)
    files.add_argument("--limit", default=25, type=int)
    files.add_argument("--before", type=int)
    attach = actions.add_parser("attach", help="upload a file's original bytes to a repair")
    attach.add_argument("id", type=int)
    attach.add_argument("file", type=Path)
    attach.add_argument("--name", help="display file name (default: the file's own name)")
    attach.add_argument("--entry", type=int, help="link to a visible work entry of this repair")
    attach.add_argument("--key", required=True)
    download = actions.add_parser("download", help="download original bytes and verify SHA-256")
    download.add_argument("id", type=int)
    download.add_argument("attachment_id", type=int)
    download.add_argument("--output", type=Path, required=True, help="new file; never overwritten")
    detach = actions.add_parser("detach", help="detach or restore a reviewed attachment")
    detach.add_argument("id", type=int)
    detach.add_argument("attachment_id", type=int)
    add_revision(detach)
    detach.add_argument("--undo", action="store_true", help="restore a detached attachment")
    detach.add_argument("--key", required=True)
    caption = actions.add_parser("edit-attachment", help="edit a reviewed attachment caption")
    caption.add_argument("id", type=int)
    caption.add_argument("attachment_id", type=int)
    add_revision(caption)
    caption.add_argument("--caption", required=True, help="caption text; pass an empty string to clear it")
    caption.add_argument("--key", required=True)
    link_commands = []
    for action in ("link", "unlink"):
        link = actions.add_parser(f"{action}-attachment", help=f"{action} an attachment and work entry")
        link.add_argument("id", type=int)
        link.add_argument("attachment_id", type=int)
        link.add_argument("entry_id", type=int)
        add_revision(link)
        link.add_argument("--key", required=True)
        link_commands.append(link)
    charge_list = actions.add_parser("charges", help="show charges and totals (integer cents)")
    charge_list.add_argument("id", type=int)
    charge_list.add_argument("--include-consumption", action="store_true", help="include linked stock movements")
    add_charge = actions.add_parser("add-charge", help="add a free-text part or labor charge")
    add_charge.add_argument("id", type=int)
    add_charge.add_argument("--kind", choices=("part", "labor"), required=True)
    add_charge.add_argument("--description", required=True)
    add_charge.add_argument("--quantity", required=True, help="decimal, up to 3 places")
    add_charge.add_argument("--unit", default="")
    add_charge.add_argument("--unit-price", required=True, help="EUR, up to 2 decimal places")
    add_charge.add_argument("--unit-cost", help="EUR, optional")
    add_charge.add_argument("--notes", default="")
    add_charge.add_argument("--key", required=True)
    consume = actions.add_parser("consume-part", help="use a catalog part and add its repair charge")
    consume.add_argument("id", type=int)
    consume.add_argument("item_id", type=int)
    add_revision(consume, "--revision", "--repair-revision", dest="repair_revision")
    add_revision(consume, "--item-revision", dest="item_revision")
    consume.add_argument("--consumed-quantity", required=True, help="quantity taken from stock")
    consume.add_argument("--billed-quantity", required=True, help="quantity used to calculate the charge")
    consume.add_argument("--unit-price", help="EUR, defaults to the reviewed catalog price")
    consume.add_argument("--key", required=True)
    return_status = actions.add_parser("part-return-status", help="review remaining consumption and revisions")
    return_status.add_argument("id", type=int)
    return_status.add_argument("charge_id", type=int)
    returns = []
    for action, help_text in (
        ("return-part", "return a physical part without changing billing"),
        ("intake-mistake", "intake mistake: remove charge and return part"),
    ):
        command = actions.add_parser(action, help=help_text, description=help_text)
        command.add_argument("id", type=int)
        command.add_argument("charge_id", type=int)
        add_revision(command, "--revision", "--repair-revision", dest="repair_revision")
        add_revision(command, "--item-revision", dest="item_revision")
        command.add_argument("--reason", required=True)
        command.add_argument("--key", required=True)
        if action == "return-part":
            command.add_argument("--quantity", required=True, help="physical return quantity, up to 3 decimals")
        else:
            add_revision(command, "--charge-revision", dest="charge_revision")
        returns.append(command)
    edit_charge = actions.add_parser("edit-charge", help="correct a reviewed charge with a reason")
    edit_charge.add_argument("id", type=int)
    edit_charge.add_argument("charge_id", type=int)
    add_revision(edit_charge)
    edit_charge.add_argument("--reason", required=True)
    edit_charge.add_argument("--kind", choices=("part", "labor"))
    for option in ("description", "quantity", "unit", "unit-price", "unit-cost", "notes"):
        edit_charge.add_argument(f"--{option}")
    edit_charge.add_argument("--clear-cost", action="store_true")
    edit_charge.add_argument("--key", required=True)
    charge_visibility = []
    for action in ("delete-charge", "restore-charge"):
        visibility = actions.add_parser(action, help="remove or restore a charge with a reason")
        visibility.add_argument("id", type=int)
        visibility.add_argument("charge_id", type=int)
        add_revision(visibility)
        visibility.add_argument("--reason", required=True)
        visibility.add_argument("--key", required=True)
        charge_visibility.append(visibility)
    discount = actions.add_parser("discount", help="set the repair discount")
    discount.add_argument("id", type=int)
    add_revision(discount, help="discount revision (0 if never set)")
    discount.add_argument("--kind", choices=money.DISCOUNT_KINDS, required=True)
    discount.add_argument("--value", default="0", help="EUR for amount, whole points for percent")
    discount.add_argument("--key", required=True)
    fees = actions.add_parser("fees", help="set the diagnosis and repair fees")
    fees.add_argument("id", type=int)
    add_revision(fees)
    fees.add_argument("--diagnosis", help="diagnosis fee in EUR")
    fees.add_argument("--repair", help="repair labour cost in EUR")
    fees.add_argument("--discount", help="discount in EUR")
    fees.add_argument("--key", required=True)
    purge = actions.add_parser("purge-attachment",
                               help="admin: delete bytes detached for 30+ days, keeping the record")
    purge.add_argument("id", type=int)
    purge.add_argument("attachment_id", type=int)
    add_revision(purge)
    purge.add_argument("--key", required=True)
    for command in (listing, create, show, label, send, edit, stage, entries, activity, add, correct, device, history,
                    files, attach, download, detach, caption, charge_list, add_charge, consume, edit_charge, discount,
                    fees, purge,
                    return_status, *returns, *flag_commands, *visibility_commands, *charge_visibility, *link_commands):
        add_common(command)


def charge_amounts(args, current=None) -> dict:
    """Convert decimal CLI text to exact integer wire fields; unchanged fields keep current values."""
    try:
        values = {} if current is None else {
            "kind": current.kind, "description": current.description,
            "quantity_milli": current.quantity_milli, "unit": current.unit,
            "unit_price_cents": current.unit_price_cents, "unit_cost_cents": current.unit_cost_cents,
            "notes": current.notes,
        }
        for name in ("kind", "description", "unit", "notes"):
            if getattr(args, name) is not None:
                values[name] = getattr(args, name)
        if args.quantity is not None:
            values["quantity_milli"] = money.parse_milli(args.quantity)
        if args.unit_price is not None:
            values["unit_price_cents"] = money.parse_cents(args.unit_price)
        if args.unit_cost is not None:
            values["unit_cost_cents"] = money.parse_cents(args.unit_cost)
        if getattr(args, "clear_cost", False):
            values["unit_cost_cents"] = None
    except ValueError as error:
        raise local.ClientError(str(error), 8) from None
    return values


def save_download(ctx, args):
    output = args.output.expanduser()
    if os.path.lexists(output):
        raise local.ClientError(f"{output} already exists; choose a new output path.", 7)
    attachment = local.show_attachment(ctx, args.id, args.attachment_id)
    with NamedTemporaryFile("wb", dir=output.parent, prefix=".repairlib-", delete=False) as temp:
        try:
            local.download_attachment(ctx, args.id, attachment, temp)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            os.unlink(temp.name)
            raise
    try:
        os.link(temp.name, output)  # refuses to replace a file created meanwhile
    except FileExistsError:
        raise local.ClientError(f"{output} already exists; choose a new output path.", 7) from None
    finally:
        os.unlink(temp.name)
    return attachment


# Each action is one small function: EXECUTE runs it against the workspace and returns a result,
# RENDER prints that result as plain text (JSON output is shared, see `emit_json`).
EXECUTE = {}
RENDER = {}


def executes(*actions):
    def register(function):
        EXECUTE.update(dict.fromkeys(actions, function))
        return function
    return register


def renders(*actions):
    def register(function):
        RENDER.update(dict.fromkeys(actions, function))
        return function
    return register


@contextmanager
def plain_numbers():
    """Turn a malformed decimal on the command line into the invalid-input exit (8)."""
    try:
        yield
    except ValueError as error:
        raise local.ClientError(str(error), 8) from None


def details_of(args, *names, **extra) -> dict:
    return {**{name: getattr(args, name) for name in names}, **extra}


@executes("list")
def _list(ctx, args):
    return local.list_repairs(ctx, limit=args.limit, before=args.before, query=args.query, stage=args.stage,
                              picked_up=args.picked_up, archived=args.archived, technician=args.technician,
                              customer_id=args.customer, queue=args.queue)


@executes("create")
def _create(ctx, args):
    details = details_of(args, *FIELDS, warranty_until=args.warranty_until,
                         warranty_repair_id=args.warranty_repair_id, customer_id=args.customer,
                         technician_id=args.technician)
    return local.create_repair(ctx, details, args.key)


@executes("show")
def _show(ctx, args):
    return local.repair_with_names(ctx, args.id)[0]


@executes("label-options")
def _label_options(ctx, args):
    return local.label_options(ctx, args.id)


@executes("edit")
def _edit(ctx, args):
    current = local.show_repair(ctx, args.id)
    details = {name: getattr(current, name) for name in FIELDS}
    details["warranty_until"] = args.warranty_until
    details.update(customer_id=current.customer_id, technician_id=current.technician_id, revision=args.revision)
    for name in FIELDS:
        if (value := getattr(args, name)) is not None:
            details[name] = value
    if args.customer is not None:
        details["customer_id"] = args.customer
    if args.technician is not None:
        details["technician_id"] = args.technician
    if args.clear_customer:
        details["customer_id"] = None
    if args.clear_technician:
        details["technician_id"] = None
    if args.warranty_repair_id is not None:
        details["warranty_repair_id"] = args.warranty_repair_id
    return local.edit_repair(ctx, args.id, details, args.key)


@executes("pickup", "archive", "payment")
def _flag(ctx, args):
    return local.change_repair_flag(ctx, args.id, args.repair_action,
                                    {"revision": args.revision, "enabled": not args.undo}, args.key)


@executes("stage")
def _stage(ctx, args):
    return local.change_stage(ctx, args.id, details_of(args, "revision", "stage", "reason"), args.key)


@executes("entries")
def _entries(ctx, args):
    return local.list_work_entries(ctx, args.id, limit=args.limit, before=args.before)


@executes("activity")
def _activity(ctx, args):
    return local.repair_activity(ctx, args.id, limit=args.limit, before=args.before)


@executes("device-history")
def _device_history(ctx, args):
    return local.device_history(ctx, args.serial)


@executes("add-entry")
def _add_entry(ctx, args):
    return local.create_work_entry(ctx, args.id, details_of(args, "kind", "body"), args.key)


@executes("edit-entry")
def _edit_entry(ctx, args):
    return local.correct_work_entry(ctx, args.id, args.entry_id,
                                    details_of(args, "revision", "kind", "body", "reason"), args.key)


@executes("delete-entry", "restore-entry")
def _entry_visibility(ctx, args):
    return local.change_entry_visibility(ctx, args.id, args.entry_id, {
        "revision": args.revision, "deleted": args.repair_action == "delete-entry", "reason": args.reason,
    }, args.key)


@executes("entry-history")
def _entry_history(ctx, args):
    return local.work_entry_history(ctx, args.id, args.entry_id)


@executes("attachments")
def _attachments(ctx, args):
    return local.list_attachments(ctx, args.id, limit=args.limit, before=args.before)


@executes("attach")
def _attach(ctx, args):
    try:
        source = args.file.expanduser().open("rb")
    except OSError:
        raise local.ClientError("Could not read the file to attach.", 8) from None
    with source:
        return local.upload_attachment(ctx, args.id, source, filename=args.name or args.file.name,
                                       entry_id=args.entry, key=args.key)


@executes("download")
def _download(ctx, args):
    try:
        return save_download(ctx, args)
    except OSError:
        raise local.ClientError("Could not write the output file.", 3) from None


@executes("edit-attachment")
def _edit_attachment(ctx, args):
    return local.edit_attachment_caption(ctx, args.id, args.attachment_id,
                                         details_of(args, "revision", "caption"), args.key)


@executes("link-attachment", "unlink-attachment")
def _link_attachment(ctx, args):
    return local.change_attachment_link(ctx, args.id, args.attachment_id, {
        "revision": args.revision, "entry_id": args.entry_id, "linked": args.repair_action == "link-attachment",
    }, args.key)


@executes("purge-attachment")
def _purge_attachment(ctx, args):
    return local.purge_attachment(ctx, args.id, args.attachment_id, {"revision": args.revision}, args.key)


@executes("detach")
def _detach(ctx, args):
    return local.change_attachment_detached(ctx, args.id, args.attachment_id,
                                            {"revision": args.revision, "detached": not args.undo}, args.key)


@executes("charges")
def _charges(ctx, args):
    return local.charge_summary(ctx, args.id, include_consumption=args.include_consumption)


@executes("fees")
def _fees(ctx, args):
    details = {}
    for name, value in (("diagnosis", args.diagnosis), ("labor", args.repair),
                        ("discount", args.discount)):
        if value is not None:
            details[name] = money.parse_cents(value)
    return local.set_fees(ctx, args.id, details, args.revision, args.key)


@executes("add-charge")
def _add_charge(ctx, args):
    return local.create_charge(ctx, args.id, charge_amounts(args), args.key)


@executes("consume-part")
def _consume_part(ctx, args):
    with plain_numbers():
        details = {
            "item_id": args.item_id, "repair_revision": args.repair_revision, "item_revision": args.item_revision,
            "consumed_quantity_milli": money.parse_milli(args.consumed_quantity),
            "billed_quantity_milli": money.parse_milli(args.billed_quantity),
            "unit_price_cents": None if args.unit_price is None else money.parse_cents(args.unit_price),
        }
    return local.consume_part(ctx, args.id, details, args.key)


@executes("part-return-status")
def _part_return_status(ctx, args):
    return local.part_return_status(ctx, args.id, args.charge_id)


@executes("return-part", "intake-mistake")
def _return_part(ctx, args):
    combined = args.repair_action == "intake-mistake"
    details = details_of(args, "repair_revision", "item_revision", "reason")
    if combined:
        details["charge_revision"] = args.charge_revision
    else:
        with plain_numbers():
            details["quantity_milli"] = money.parse_milli(args.quantity)
    return local.return_part(ctx, args.id, args.charge_id, details, args.key, intake_mistake=combined)


@executes("edit-charge")
def _edit_charge(ctx, args):
    current = local.show_charge(ctx, args.id, args.charge_id)
    return local.correct_charge(ctx, args.id, args.charge_id, {
        **charge_amounts(args, current), "revision": args.revision, "reason": args.reason,
    }, args.key)


@executes("delete-charge", "restore-charge")
def _charge_visibility(ctx, args):
    return local.change_charge_visibility(ctx, args.id, args.charge_id, {
        "revision": args.revision, "deleted": args.repair_action == "delete-charge", "reason": args.reason,
    }, args.key)


@executes("discount")
def _discount(ctx, args):
    with plain_numbers():
        value = (0 if args.kind == "none" else money.parse_cents(args.value)
                 if args.kind == "amount" else money.parse_percent(args.value))
    return local.set_discount(ctx, args.id, {"revision": args.revision, "kind": args.kind, "value": value}, args.key)


def emit_json(result, args) -> None:
    if isinstance(result, ChargeSummary) and not getattr(args, "include_consumption", False):
        payload = CHARGE_SUMMARY.dump_python(result, exclude=SUMMARY_CONSUMPTION_FIELDS)
    elif isinstance(result, Charge) and args.repair_action != "consume-part":
        payload = CHARGE.dump_python(result, exclude=CHARGE_CONSUMPTION_FIELDS)
    else:
        payload = [asdict(item) for item in result] if isinstance(result, list) else asdict(result)
    print(json.dumps(payload, ensure_ascii=False))


def next_page_hint(page) -> None:
    if page.next_before is not None:
        print(f"Next page: --before {page.next_before}", file=sys.stderr)


@renders("label-options")
def _show_label_options(result, args, ctx):
    print(f"Repair revision\t{result.revision}\nPrinter revision\t{result.printer_revision}\n"
          f"Enabled\t{result.enabled}\nP15 program available\t{result.available}")


@renders("entry-history")
def _show_entry_history(result, args, ctx):
    for item in result:
        state = "deleted" if item.deleted else "visible"
        print(f"Revision {item.revision}\t{item.kind}\t{item.editor_username}\t{state}\t{item.reason}")


@renders("activity")
def _show_activity(result, args, ctx):
    for event in result.items:
        print(f"{event.occurred_at}\t{event.operation}\t{display_name(event.actor_username or '')}\t"
              f"{event.source or ''}\t{event.entity_type} {event.entity_id}")
    next_page_hint(result)


@renders("list", "entries", "attachments")
def _show_page(result, args, ctx):
    for item in result.items:
        if isinstance(item, Attachment):
            state = "detached" if item.detached else "attached"
            print(f"{item.id}\t{item.filename}\t{item.size_bytes}\t{state}\t{item.sha256}")
        elif isinstance(item, WorkEntry):
            state = " (deleted)" if item.deleted else ""
            print(f"{item.id}\t{item.kind}\t{display_name(item.author_username)}\t{item.body}{state}")
        else:
            print(f"{item.id}\t{item.device_type}\t{item.model}\t{item.stage}")
    next_page_hint(result)


@renders("device-history")
def _show_device_history(result, args, ctx):
    for item in result:
        print(f"{item.id}\t{item.created_at}\t{item.stage}\t{item.reported_issue}")


@renders("add-entry")
def _show_added_entry(result, args, ctx):
    print(f"Added work entry {result.id} by {display_name(result.author_username)}.")


@renders("charges", "discount", "fees")
def _show_charges(result, args, ctx):
    for item in result.items:
        state = " (removed)" if item.deleted else ""
        consumed = (f"\tconsumed {money.format_milli(item.consumed_quantity_milli)} {item.unit}"
                    if item.movement_id is not None else "")
        print(f"{item.id}\t{item.kind}\t{item.description}{state}\t"
              f"{money.format_milli(item.quantity_milli)} {item.unit}\t"
              f"{money.format_cents(item.unit_price_cents)}\t{money.format_cents(item.line_total_cents)}"
              f"{consumed}")
    fees = local.ticket_fees(ctx, args.id)
    # Every labour charge above the diagnosis is repair work, including free-text labour rows.
    repair = result.labor_subtotal_cents - fees["diagnosis"]
    print(f"Diagnosis {money.format_cents(fees['diagnosis'])}, repair {money.format_cents(repair)}, "
          f"parts {money.format_cents(fees['parts'])}, discount {money.format_cents(fees['discount_cents'])}, "
          f"total {money.format_cents(fees['total_cents'])} EUR (discount revision {result.discount_revision}).")


@renders("consume-part")
def _show_consumed(result, args, ctx):
    print(f"Charge {result.id}, movement {result.movement_id}, "
          f"consumed {money.format_milli(result.consumed_quantity_milli)} {result.unit}, "
          f"billed {money.format_milli(result.quantity_milli)} {result.unit}, "
          f"line total {money.format_cents(result.line_total_cents)} EUR.")


@renders("part-return-status")
def _show_return_status(result, args, ctx):
    print(f"{result.description}\tconsumption movement {result.movement_id}\n"
          f"Remaining to return: {money.format_milli(result.remaining_quantity_milli)} {result.unit}\n"
          f"Repair revision {result.repair_revision}, item revision {result.item_revision}, "
          f"charge revision {result.charge_revision}.")


@renders("return-part", "intake-mistake")
def _show_return(result, args, ctx):
    movement = result.stock.movement
    print(f"Return movement {movement.id}, returned {money.format_milli(movement.quantity_milli)} "
          f"{result.stock.item.unit}, on hand {money.format_milli(movement.on_hand_after_milli)}, "
          f"repair total {money.format_cents(result.totals.total_cents)} EUR.")


@renders("add-charge", "edit-charge", "delete-charge", "restore-charge")
def _show_charge(result, args, ctx):
    print(f"Charge {result.id}, revision {result.revision}, line total "
          f"{money.format_cents(result.line_total_cents)} EUR, removed={result.deleted}.")


@renders("attach", "download", "detach", "edit-attachment", "link-attachment", "unlink-attachment",
         "purge-attachment")
def _show_attachment(result, args, ctx):
    state = "detached" if result.detached else "attached"
    saved = f" Saved to {args.output}." if args.repair_action == "download" else ""
    print(f"Attachment {result.id}, revision {result.revision}, {state}, sha256 {result.sha256}.{saved}")


@renders("edit-entry", "delete-entry", "restore-entry")
def _show_entry(result, args, ctx):
    print(f"Work entry {result.id}, revision {result.revision}, deleted={result.deleted}.")


@renders("show")
def _show_repair(result, args, ctx):
    names = local.repair_with_names(ctx, result.id)[1]
    summary = local.charge_summary(ctx, result.id)
    device = " ".join(part for part in (result.device_type, result.brand, result.model) if part)
    print(f"Repair\t{result.id}\nRevision\t{result.revision}")
    print(f"Device\t{device or result.display_device or 'None'}")
    print(f"Customer\t{display_name(names['customer']) if names['customer'] else 'None'}")
    print(f"Technician\t{display_name(names['technician']) if names['technician'] else 'Unassigned'}")
    print(f"Reported issue\t{display_name(result.reported_issue)}")
    print(f"Stage\t{result.stage}")
    if result.warranty_until:
        print(f"Warranty end date\t{result.warranty_until}")
    if result.warranty_repair_id:
        print(f"Warranty return of\t{result.warranty_repair_id}")
    print(f"Pickup\t{'Picked up ' + result.picked_up_at if result.picked_up_at else 'In shop'}")
    print(f"Archive\t{'Archived ' + result.archived_at if result.archived_at else 'Active'}")
    fees = local.ticket_fees(ctx, result.id)
    print(f"Diagnosis\t{money.format_cents(fees['diagnosis'])} EUR")
    print(f"Repair cost\t{money.format_cents(summary.labor_subtotal_cents - fees['diagnosis'])} EUR")
    print(f"Parts\t{money.format_cents(summary.parts_subtotal_cents)} EUR")
    print(f"Discount\t{money.format_cents(summary.discount_cents)} EUR")
    print(f"Total\t{money.format_cents(summary.total_cents)} EUR (not a payment)")


def show_default(result, args, ctx):
    """create, edit, stage, pickup and archive all return the repair."""
    print(f"Repair {result.id}, revision {result.revision}, stage {result.stage}.")


def run_repair_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.repair_action == "print-label":
            receipt = local.send_label(ctx, args.revision, args.key,
                                       repair_id=args.id, printer_revision=args.printer_revision)
            return label_result(receipt, args.json)
        result = EXECUTE[args.repair_action](ctx, args)
        if args.json:
            emit_json(result, args)
        else:
            RENDER.get(args.repair_action, show_default)(result, args, ctx)
    except (OSError, ValueError):
        print("repairs: Could not read the workspace.", file=sys.stderr)
        return 1
    return 0
