"""Local workspace operations for the CLI and stdio MCP.

Both tools run on the shop machine and call the shared domain operations
directly as the built-in `cli` or `mcp` administrator. Shell access to the
workspace already implies full control of its files, so no token is needed;
the audit trail still names the tool that made each change.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import shutil
from typing import BinaryIO

from pydantic import TypeAdapter, ValidationError

from repairlib.config import resolve_workspace
from repairlib.contracts import (
    Attachment, AttachmentPage, Charge, ChargeSummary, CsvApply, CsvPreview, CustomerHistoryPage,
    CustomerPage, CustomerResponse, Dashboard, DeviceRepair, InventoryItem, InventoryPage, LabelOptions, MovementPage,
    PartReturnResult, PartReturnStatus, PortableImport, PortablePreview, PrintReceipt, PrinterSettings, Repair,
    RepairActivityPage, RepairPage, ShopSettings, StockChange, StockReconciliation, User, UserPage, WorkEntry,
    WorkEntryPage, WorkEntryRevision,
    CHARGE_CONSUMPTION_FIELDS, PORTABLE_MAX_BYTES, AttachmentCaption, AttachmentDetach, AttachmentLink, AttachmentPurge,
    ChargeInput, ChargePatch, ChargeVisibility, Customer, CustomerAnonymize, CustomerArchiveChange, CustomerExport, CustomerInput, CustomerPatch,
    DiscountChange, IntakeMistake, ItemInput, ItemPatch, PartConsumption,
    PartReturn, PrinterChange, RepairFlagChange, RepairInput, RepairPatch, SettingsPatch, StageChange,
    StockAdjustment, StockReceipt, StockReversal, UserInput, UserPatch, WorkEntryCorrection, WorkEntryInput,
    WorkEntryVisibility,
)
from repairlib.domain import (
    attachments, charges, csv_intake, customer_privacy, customers, dashboard, inventory, portable_shop, printing,
    repairs, settings, stock, users,
)
from repairlib.domain.customer_csv import CUSTOMERS
from repairlib.domain.customers import CustomerAccess, CustomerError, StorageError, StorageOperationalError
from repairlib.domain.inventory_csv import CATALOG, OPENING_STOCK
from repairlib.lifecycle.workspace import WorkspaceError, maintenance_lock, workspace_status

CSV_INTAKES = {intake.kind: intake for intake in (CUSTOMERS, CATALOG, OPENING_STOCK)}
CSV_KINDS = tuple(CSV_INTAKES)

# Exit codes are part of the CLI contract: 4 unauthenticated, 5 forbidden,
# 6 not found, 7 conflict, 8 invalid fields, 3 unavailable.
EXIT_CODES = {401: 4, 403: 5, 404: 6, 409: 7, 413: 8, 415: 8, 422: 8}
MESSAGES = {
    "invalid_archive": "The portable archive is invalid.",
    "export_unavailable": "Could not create a verified portable archive.",
    "not_inventory_linked": "This charge has no inventory consumption.",
    "purged": "The attachment bytes were purged.",
    "file_unavailable": "The attachment file is unavailable.",
    "archived": "The repair or inventory item is archived. Restore it before changing it.",
    "deleted": "That work entry or charge is deleted. Restore it before changing it.",
    "insufficient_stock": "Not enough stock on hand for this change.",
    "invalid_transition": "That stage change is not allowed from the current stage.",
    "duplicate_sku": "An inventory item with this SKU already exists.",
    "stale_revision": "The record changed after you reviewed it. Read it again and retry with the new revision.",
    "same_state": "The record already has the requested state.",
    "same_stage": "The repair is already in this stage.",
    "not_reversible": "That stock movement cannot be reversed.",
    "already_reversed": "That stock movement is already reversed.",
    "unit_locked": "The unit is locked because the item already has stock movements.",
    "too_many_charges": "The repair has reached the maximum number of charge lines.",
    "discount_exceeds_subtotal": "The discount cannot exceed the subtotal.",
    "total_out_of_range": "The charge total is outside the supported range.",
    "inventory_linked": "Catalog details are fixed on a stock-linked charge; change its billing fields only.",
    "detached": "That attachment is detached. Restore it before changing it.",
    "retention": "Only attachments detached for at least 30 days can be purged.",
    "printer_disabled": "Direct label printing is disabled for this shop.",
    "printer_unavailable": "The P15 program is unavailable on this machine.",
}


class ClientError(Exception):
    def __init__(self, message: str, exit_code: int = 1, *, code: str = "client_error",
                 http_status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.http_status = http_status
        self.request_id = request_id


@dataclass(frozen=True)
class Local:
    workspace: object
    access: CustomerAccess


def open_workspace(path=None, actor: str = "cli") -> Local:
    workspace = resolve_workspace(path)
    try:
        status = workspace_status(workspace)
    except (OSError, WorkspaceError):
        status = "invalid"
    if status != "initialized":
        raise ClientError(f"The workspace is not ready ({status}). Check --workspace or run repairlib migrate.", 3,
                          code="unavailable")
    with local_lock(workspace):
        if workspace_status(workspace) != "initialized":
            raise ClientError("The workspace is not ready. Check --workspace or run repairlib migrate.",
                              3, code="unavailable")
        call(users.ensure_local_actor, workspace, actor)
    return Local(workspace, CustomerAccess(actor))


@contextmanager
def local_lock(workspace):
    try:
        with maintenance_lock(workspace, exclusive=False, create=False):
            yield
    except WorkspaceError as error:
        raise ClientError(str(error), 3, code="busy") from None
    except OSError:
        raise ClientError("Could not open the workspace lock.", 3, code="unavailable") from None


def domain_error(error: CustomerError) -> ClientError:
    exit_code = 7 if error.code in {"printer_disabled", "printer_unavailable"} else EXIT_CODES.get(error.status, 3)
    message = MESSAGES.get(error.code, str(error))
    if error.fields:
        message += " " + " ".join(f"{name}: {text}" for name, text in error.fields.items())
    return ClientError(message, exit_code, code=error.code, http_status=error.status)


def call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except CustomerError as error:
        raise domain_error(error) from None
    except StorageOperationalError as error:
        if "locked" in str(error) or "busy" in str(error):
            raise ClientError("The workspace is busy. Retry with the same key.", 3, code="busy") from None
        raise ClientError("The workspace could not complete the request.", 3, code="server_error") from None
    except StorageError:
        raise ClientError("The workspace could not complete the request.", 3, code="server_error") from None


ADAPTERS: dict[type, TypeAdapter] = {}


def parse(shape: type, details: dict):
    """Validate a request body exactly as the JSON API does (strict JSON types)."""
    adapter = ADAPTERS.setdefault(shape, TypeAdapter(shape))
    try:
        return adapter.validate_json(json.dumps(details), strict=True)
    except ValidationError:
        raise ClientError("Check the request fields.", 8, code="validation") from None


def run(ctx: Local, operation, *args, **kwargs):
    with local_lock(ctx.workspace):
        return call(operation, ctx.workspace, ctx.access, *args, **kwargs)


# Shop, users and settings

def read_dashboard(ctx) -> Dashboard:
    return run(ctx, dashboard.read_dashboard)


def list_users(ctx, *, limit, before) -> UserPage:
    return run(ctx, users.list_users, before=before, limit=limit)


def create_user(ctx, details, key) -> User:
    return run(ctx, users.create_user, parse(UserInput, details), key=key)


def show_user(ctx, user_id) -> User:
    return run(ctx, users.read_user, user_id)


def edit_user(ctx, user_id, details, key) -> User:
    return run(ctx, users.update_user, user_id, parse(UserPatch, details), key=key)


def set_password(ctx, username, password) -> User:
    return run(ctx, users.set_password, username, password)


def show_settings(ctx) -> ShopSettings:
    return run(ctx, settings.read_settings)


def show_settings_page(ctx) -> tuple[ShopSettings, int]:
    return run(ctx, settings.read_settings_page)


def edit_settings(ctx, details, key, diagnosis_fee_cents=None) -> ShopSettings:
    return run(ctx, settings.update_settings, parse(SettingsPatch, details), key=key,
               diagnosis_fee_cents=diagnosis_fee_cents)


def edit_settings_page(ctx, details, key, diagnosis_fee_cents=None) -> tuple[ShopSettings, int | None]:
    return run(ctx, settings.update_settings_page, parse(SettingsPatch, details), key=key,
               diagnosis_fee_cents=diagnosis_fee_cents)


def show_printer(ctx) -> PrinterSettings:
    return run(ctx, printing.read_printer)


def configure_printer(ctx, driver, revision, key) -> PrinterSettings:
    return run(ctx, printing.configure_printer, parse(PrinterChange, {"driver": driver, "revision": revision}),
               key=key)


def label_options(ctx, repair_id) -> LabelOptions:
    return run(ctx, printing.read_label_options, repair_id)


def send_label(ctx, revision, key, *, repair_id=None, printer_revision=None) -> PrintReceipt:
    if repair_id is None:
        return run(ctx, printing.print_p15, printer_revision=revision, key=key)
    return run(ctx, printing.print_p15, repair_id=repair_id, repair_revision=revision,
               printer_revision=printer_revision, key=key)


# Customers

def list_customers(ctx, *, limit, before, query="", include_archived=False) -> CustomerPage:
    return run(ctx, customers.list_customers, before=before, limit=limit, query=query,
               include_archived=include_archived)[0]


def create_customer(ctx, details, key) -> CustomerResponse:
    return run(ctx, customers.create_customer, parse(CustomerInput, details), key=key)


def show_customer(ctx, customer_id) -> CustomerResponse:
    return run(ctx, customers.read_customer, customer_id)[0]


def edit_customer(ctx, customer_id, details, key) -> CustomerResponse:
    return run(ctx, customers.update_customer, customer_id, parse(CustomerPatch, details), key=key)


def change_customer_archive(ctx, customer_id, *, revision, enabled, key) -> CustomerResponse:
    change = parse(CustomerArchiveChange, {"revision": revision, "enabled": enabled})
    return run(ctx, customers.change_customer_archive, customer_id, change, key=key)


def anonymize_customer(ctx, customer_id, *, revision, key) -> Customer:
    return run(ctx, customer_privacy.anonymize_customer, customer_id,
               parse(CustomerAnonymize, {"revision": revision}), key=key)


def export_customer(ctx, customer_id) -> CustomerExport:
    return run(ctx, customer_privacy.export_customer, customer_id)


def customer_history(ctx, customer_id, *, limit, before) -> CustomerHistoryPage:
    return run(ctx, customers.read_customer_history, customer_id, before=before, limit=limit)[1]


# Repairs and work entries

def list_repairs(ctx, *, limit, before, query="", stage=None, picked_up=None, archived=None,
                 technician="", customer_id=None, queue="", paid=None) -> RepairPage:
    return run(ctx, repairs.list_repairs, before=before, limit=limit, query=query, stage=stage,
               picked_up=picked_up, archived=archived, technician=technician, customer_id=customer_id,
               queue=queue, paid=paid)[0]


def create_repair(ctx, details, key) -> Repair:
    return run(ctx, repairs.create_repair, parse(RepairInput, details), key=key)


def show_repair(ctx, repair_id) -> Repair:
    return run(ctx, repairs.read_repair, repair_id)[0]


def repair_with_names(ctx, repair_id):
    """Return `(repair, names)` where names holds customer and technician display names."""
    repair, names, _ = run(ctx, repairs.read_repair, repair_id)
    return repair, names


def repair_activity(ctx, repair_id, *, limit=25, before=None) -> RepairActivityPage:
    return run(ctx, repairs.repair_activity, repair_id, before=before, limit=limit)


def device_history(ctx, serial) -> list[DeviceRepair]:
    return run(ctx, repairs.device_history, serial)


def edit_repair(ctx, repair_id, details, key) -> Repair:
    return run(ctx, repairs.update_repair, repair_id, parse(RepairPatch, details), key=key)


def change_stage(ctx, repair_id, details, key) -> Repair:
    return run(ctx, repairs.change_stage, repair_id, parse(StageChange, details), key=key)


def change_repair_flag(ctx, repair_id, flag, details, key) -> Repair:
    return run(ctx, repairs.change_repair_flag, repair_id, parse(RepairFlagChange, details), key=key, flag=flag)


def list_work_entries(ctx, repair_id, *, limit, before) -> WorkEntryPage:
    return run(ctx, repairs.list_work_entries, repair_id, before=before, limit=limit)[0]


def create_work_entry(ctx, repair_id, details, key) -> WorkEntry:
    return run(ctx, repairs.create_work_entry, repair_id, parse(WorkEntryInput, details), key=key)


def correct_work_entry(ctx, repair_id, entry_id, details, key) -> WorkEntry:
    return run(ctx, repairs.correct_work_entry, repair_id, entry_id, parse(WorkEntryCorrection, details), key=key)


def change_entry_visibility(ctx, repair_id, entry_id, details, key) -> WorkEntry:
    return run(ctx, repairs.change_entry_visibility, repair_id, entry_id, parse(WorkEntryVisibility, details),
               key=key)


def work_entry_history(ctx, repair_id, entry_id) -> list[WorkEntryRevision]:
    return run(ctx, repairs.work_entry_history, repair_id, entry_id)


# Attachments

def list_attachments(ctx, repair_id, *, limit, before) -> AttachmentPage:
    return run(ctx, attachments.list_attachments, repair_id, before=before, limit=limit)[0]


def upload_attachment(ctx, repair_id, source: bytes | BinaryIO, *, filename, entry_id, key) -> Attachment:
    """Stage original bytes (or a binary file) and commit them as one attachment."""
    with local_lock(ctx.workspace):
        staged = call(attachments.prepare_upload, ctx.workspace, ctx.access, repair_id, filename, entry_id)
        try:
            if isinstance(source, bytes):
                call(attachments.write_chunk, staged, source)
            else:
                while chunk := source.read(1 << 20):
                    call(attachments.write_chunk, staged, chunk)
        except BaseException:
            staged.discard()
            raise
        return call(attachments.commit_upload, ctx.workspace, ctx.access, repair_id, staged,
                    filename=filename, entry_id=entry_id, key=key)


def show_attachment(ctx, repair_id, attachment_id) -> Attachment:
    return run(ctx, attachments.read_attachment, repair_id, attachment_id)


def download_attachment(ctx, repair_id, attachment, destination, *, max_bytes=None) -> None:
    """Copy original bytes to a binary writer, refusing size or SHA-256 drift."""
    found, source = run(ctx, attachments.attachment_content, repair_id, attachment.id, inline=False)
    digest, size = sha256(), 0
    with source:
        if max_bytes is not None and found.size_bytes > max_bytes:
            raise ClientError("The attachment exceeds the content size limit.", code="attachment_too_large")
        while size < found.size_bytes and (chunk := source.read(min(1 << 20, found.size_bytes - size))):
            size += len(chunk)
            digest.update(chunk)
            destination.write(chunk)
        if source.read(1):
            size += 1
    if size != found.size_bytes or digest.hexdigest() != found.sha256:
        raise ClientError("The stored bytes do not match the recorded SHA-256.", 3, code="invalid_response")


def edit_attachment_caption(ctx, repair_id, attachment_id, details, key) -> Attachment:
    return run(ctx, attachments.edit_caption, repair_id, attachment_id, parse(AttachmentCaption, details), key=key)


def change_attachment_link(ctx, repair_id, attachment_id, details, key) -> Attachment:
    return run(ctx, attachments.change_entry_link, repair_id, attachment_id, parse(AttachmentLink, details),
               key=key)


def change_attachment_detached(ctx, repair_id, attachment_id, details, key) -> Attachment:
    return run(ctx, attachments.change_detached, repair_id, attachment_id, parse(AttachmentDetach, details),
               key=key)


def purge_attachment(ctx, repair_id, attachment_id, details, key) -> Attachment:
    return run(ctx, attachments.purge_attachment, repair_id, attachment_id, parse(AttachmentPurge, details),
               key=key)


# Charges

def without_consumption(charge):
    return replace(charge, **{name: None for name in CHARGE_CONSUMPTION_FIELDS})


def charge_summary(ctx, repair_id, *, include_consumption=False) -> ChargeSummary:
    summary = run(ctx, charges.charge_summary, repair_id)[0]
    if include_consumption:
        return summary
    return replace(summary, items=[without_consumption(item) for item in summary.items])


def ticket_fees(ctx, repair_id) -> dict:
    return run(ctx, charges.ticket_fees, repair_id)[0]


def set_fees(ctx, repair_id, details, revision, key) -> ChargeSummary:
    run(ctx, charges.set_fees, repair_id, diagnosis=details.get("diagnosis"),
        labor=details.get("labor"), discount=details.get("discount"), revision=revision, key=key)
    return charge_summary(ctx, repair_id)


def show_charge(ctx, repair_id, charge_id, *, include_consumption=False) -> Charge:
    charge = run(ctx, charges.read_charge, repair_id, charge_id)[0]
    return charge if include_consumption else without_consumption(charge)


def create_charge(ctx, repair_id, details, key) -> Charge:
    return without_consumption(run(ctx, charges.create_charge, repair_id, parse(ChargeInput, details), key=key))


def consume_part(ctx, repair_id, details, key) -> Charge:
    return run(ctx, stock.consume_for_repair, repair_id, parse(PartConsumption, details), key=key)


def part_return_status(ctx, repair_id, charge_id) -> PartReturnStatus:
    return run(ctx, stock.part_return_status, repair_id, charge_id)


def return_part(ctx, repair_id, charge_id, details, key, *, intake_mistake=False) -> PartReturnResult:
    if intake_mistake:
        return run(ctx, stock.correct_intake_mistake, repair_id, charge_id, parse(IntakeMistake, details), key=key)
    return run(ctx, stock.return_part, repair_id, charge_id, parse(PartReturn, details), key=key)


def correct_charge(ctx, repair_id, charge_id, details, key) -> Charge:
    return without_consumption(run(ctx, charges.correct_charge, repair_id, charge_id, parse(ChargePatch, details),
                                   key=key))


def change_charge_visibility(ctx, repair_id, charge_id, details, key) -> Charge:
    return without_consumption(run(ctx, charges.change_charge_visibility, repair_id, charge_id,
                                   parse(ChargeVisibility, details), key=key))


def set_discount(ctx, repair_id, details, key) -> ChargeSummary:
    summary = run(ctx, charges.set_discount, repair_id, parse(DiscountChange, details), key=key)
    return replace(summary, items=[without_consumption(item) for item in summary.items])



# Inventory

def list_inventory(ctx, *, limit, before, include_archived=False, low_stock=False, query="", category="") -> InventoryPage:
    return run(ctx, inventory.list_items, before=before, limit=limit, include_archived=include_archived,
               low_stock=low_stock, query=query, category=category)


def create_inventory_item(ctx, details, key) -> InventoryItem:
    return run(ctx, inventory.create_item, parse(ItemInput, details), key=key)


def show_inventory_item(ctx, item_id) -> InventoryItem:
    return run(ctx, inventory.read_item, item_id)


def edit_inventory_item(ctx, item_id, details, key) -> InventoryItem:
    patch = parse(ItemPatch, details)
    return run(ctx, inventory.edit_item, item_id, patch, revision=patch.revision, key=key)


def archive_inventory_item(ctx, item_id, *, revision, key) -> InventoryItem:
    return run(ctx, inventory.archive_item, item_id, revision=revision, key=key)


def restore_inventory_item(ctx, item_id, *, revision, key) -> InventoryItem:
    return run(ctx, inventory.restore_item, item_id, revision=revision, key=key)


def change_stock(ctx, item_id, action, details, key) -> StockChange:
    """Apply one `receive`, `adjust` or `movements/<id>/reverse` command to an item."""
    if action == "receive":
        receipt = parse(StockReceipt, details)
        return run(ctx, stock.receive_stock, item_id, receipt.quantity_milli, revision=receipt.revision,
                   key=key, reason=receipt.reason)
    if action == "adjust":
        adjustment = parse(StockAdjustment, details)
        return run(ctx, stock.adjust_stock, item_id, adjustment.quantity_milli, adjustment.reason,
                   revision=adjustment.revision, key=key)
    movement_id = int(action.removeprefix("movements/").removesuffix("/reverse"))
    return reverse_stock_movement(ctx, item_id, movement_id, details, key)


def reverse_stock_movement(ctx, item_id, movement_id, details, key) -> StockChange:
    reversal = parse(StockReversal, details)
    return run(ctx, stock.reverse_movement, item_id, movement_id, reversal.reason, revision=reversal.revision,
               key=key)


def list_movements(ctx, item_id, *, limit, before) -> MovementPage:
    return run(ctx, stock.list_movements, item_id, before=before, limit=limit)


def reconcile_stock(ctx) -> StockReconciliation:
    return run(ctx, stock.reconcile_stock)


# Portable shop archives and CSV intake

def download_portable_shop(ctx, destination: BinaryIO) -> int:
    handle, size = run(ctx, portable_shop.export_archive)
    with handle:
        shutil.copyfileobj(handle, destination)
    return size


def check_archive_size(size: int) -> None:
    if not 0 < size <= PORTABLE_MAX_BYTES:
        raise ClientError("The portable archive is empty or exceeds its size limit.", 8, code="archive_too_large")


def preview_portable_shop(ctx, source: BinaryIO, size: int) -> PortablePreview:
    check_archive_size(size)
    reviewed_shop = run(ctx, portable_shop.authorize_preview)
    return run(ctx, portable_shop.preview_archive, source, reviewed_shop=reviewed_shop)


def import_portable_shop(ctx, source: BinaryIO, size: int, *, destination_shop_id, revision, key) -> PortableImport:
    check_archive_size(size)
    run(ctx, portable_shop.authorize_import, destination_shop_id)
    return run(ctx, portable_shop.import_archive, source, destination_shop_id=destination_shop_id,
               revision=revision, key=key)


def csv_intake_kind(kind: str):
    if kind not in CSV_INTAKES:
        raise ClientError("Choose a supported CSV kind.", 2)
    return CSV_INTAKES[kind]


def csv_template(ctx, kind) -> bytes:
    content = run(ctx, csv_intake.template, csv_intake_kind(kind))
    return content.encode() if isinstance(content, str) else content


def preview_csv(ctx, kind, raw: bytes) -> CsvPreview:
    intake = csv_intake_kind(kind)
    run(ctx, csv_intake.authorize_upload)
    return run(ctx, csv_intake.preview, intake, raw)


def apply_csv(ctx, kind, raw: bytes, *, preview_id, input_hash, key) -> CsvApply:
    intake = csv_intake_kind(kind)
    run(ctx, csv_intake.authorize_upload)
    return run(ctx, csv_intake.apply, intake, raw, preview_id=preview_id, input_hash=input_hash, key=key)
