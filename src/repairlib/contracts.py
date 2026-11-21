"""Wire types shared by the HTTP adapters and client."""

from dataclasses import dataclass, field as dataclass_field
import re
from typing import Literal

API_TOKEN_FORMAT = re.compile(r"rl_[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class TokenIdentity:
    shop_id: int
    user_id: int
    username: str
    role: Literal["admin", "technician", "viewer"]
    scope: Literal["read", "write"]
    token_name: str
    kind: Literal["user", "agent"] = "user"
    preset: str | None = None


@dataclass(frozen=True)
class CustomerInput:
    __pydantic_config__ = {"extra": "forbid"}

    name: str
    phone: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""


@dataclass(frozen=True, kw_only=True)
class Customer(CustomerInput):
    id: int
    revision: int
    created_at: str
    archived_at: str | None = None


@dataclass(frozen=True, kw_only=True)
class CustomerWithNotices(Customer):
    contact_warnings: dict[str, str]
    possible_duplicate_id: int | None = None


CustomerResponse = CustomerWithNotices | Customer


@dataclass(frozen=True)
class CustomerListRow(Customer):
    open_repair_count: int | None = None


@dataclass(frozen=True)
class CustomerPage:
    items: list[CustomerListRow]
    next_before: int | None


@dataclass(frozen=True)
class CustomerHistoryEvent:
    id: int
    operation: str
    occurred_at: str
    actor_user_id: int
    actor_username: str
    source: str
    token_label: str | None
    actor_kind: str
    changes: dict[str, str | list[str] | dict[str, str | None]]


@dataclass(frozen=True)
class CustomerHistoryPage:
    items: list[CustomerHistoryEvent]
    next_before: int | None


@dataclass(frozen=True)
class CustomerPatch:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CustomerAnonymize:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int


@dataclass(frozen=True)
class CustomerArchiveChange:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    enabled: bool


@dataclass(frozen=True)
class UserInput:
    __pydantic_config__ = {"extra": "forbid"}

    username: str
    role: Literal["admin", "technician", "viewer"]
    password: str = dataclass_field(repr=False)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: Literal["admin", "technician", "viewer"]
    active: bool
    revision: int


@dataclass(frozen=True)
class UserPage:
    items: list[User]
    next_before: int | None


@dataclass(frozen=True)
class UserPatch:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    username: str | None = None
    role: Literal["admin", "technician", "viewer"] | None = None
    active: bool | None = None
    password: str | None = dataclass_field(default=None, repr=False)


@dataclass(frozen=True)
class ShopSettings:
    name: str
    timezone: str
    revision: int
    max_upload_mib: int = 256
    inline_preview_mib: int = 8
    language: str = ""
    address: str = ""
    phone: str = ""
    terms: str = ""


@dataclass(frozen=True)
class SettingsPatch:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    name: str | None = None
    timezone: str | None = None
    max_upload_mib: int | None = None
    inline_preview_mib: int | None = None
    language: str | None = None
    address: str | None = None
    phone: str | None = None
    terms: str | None = None


@dataclass(frozen=True)
class PrinterSettings:
    driver: Literal["none", "p15"]
    revision: int
    available: bool


@dataclass(frozen=True)
class PrinterChange:
    __pydantic_config__ = {"extra": "forbid"}

    driver: Literal["none", "p15"]
    revision: int


@dataclass(frozen=True)
class PrinterTest:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int


@dataclass(frozen=True)
class LabelPrint:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    printer_revision: int


@dataclass(frozen=True)
class LabelOptions:
    revision: int
    printer_revision: int
    enabled: bool
    available: bool


@dataclass(frozen=True)
class PrintReceipt:
    outcome: Literal["acknowledged", "uncertain", "failed", "unavailable"]
    message: str


@dataclass(frozen=True)
class RepairInput:
    __pydantic_config__ = {"extra": "forbid"}

    customer_id: int | None = None
    technician_id: int | None = None
    device_type: str = ""
    brand: str = ""
    model: str = ""
    serial: str = ""
    display_device: str = ""
    reported_issue: str = ""
    notes: str = ""
    # An omitted edit keeps the existing date. An empty string clears it.
    warranty_until: str | None = None
    # The earlier repair this one returns under warranty.
    warranty_repair_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class Repair(RepairInput):
    id: int
    stage: Literal["new", "in_progress", "waiting_parts", "lab_test", "customer_test", "repaired", "no_repair"]
    revision: int
    created_at: str
    picked_up_at: str | None = None
    archived_at: str | None = None
    payment_paid: bool | None = None

    def __post_init__(self):
        if self.payment_paid is not None:  # SQLite returns 0/1.
            object.__setattr__(self, "payment_paid", bool(self.payment_paid))


@dataclass(frozen=True)
class RepairListRow(Repair):
    customer_name: str | None = None
    technician_username: str | None = None


@dataclass(frozen=True)
class DeviceRepair:
    id: int
    created_at: str
    stage: str
    reported_issue: str


@dataclass(frozen=True)
class RepairActivityEvent:
    id: int
    operation: str
    occurred_at: str
    actor_username: str | None
    source: str | None
    entity_type: str
    entity_id: int
    # Required, not defaulted: the API excludes default values, so a default here would hide the
    # field from HTTP while the MCP tool still returns it and the two surfaces would disagree.
    actor_kind: str


@dataclass(frozen=True)
class RepairActivityPage:
    items: list[RepairActivityEvent]
    next_before: int | None


OPEN_REPAIR_STAGES = ("in_progress", "lab_test", "customer_test", "new", "waiting_parts")
TERMINAL_REPAIR_STAGES = ("no_repair", "repaired")


@dataclass(frozen=True)
class RepairPage:
    items: list[RepairListRow]
    next_before: int | None
    total: int | None = None


@dataclass(frozen=True)
class QueueRepair:
    id: int
    customer_id: int | None
    customer_name: str | None
    device_type: str
    brand: str
    model: str
    display_device: str
    reported_issue: str
    stage: str
    picked_up_at: str | None
    timestamp: str
    technician_username: str | None = None


@dataclass(frozen=True, kw_only=True)
class RepairPatch(RepairInput):
    revision: int


@dataclass(frozen=True)
class WorkEntryInput:
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["work", "test", "contact", "handover", "note"]
    body: str


@dataclass(frozen=True)
class WorkEntry(WorkEntryInput):
    id: int
    repair_id: int
    author_id: int
    author_username: str
    created_at: str
    revision: int = 1
    deleted: bool = False


@dataclass(frozen=True)
class WorkEntryPage:
    items: list[WorkEntry]
    next_before: int | None


@dataclass(frozen=True)
class StageChange:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    stage: Literal["new", "in_progress", "waiting_parts", "lab_test", "customer_test", "repaired", "no_repair"]
    reason: str = ""


@dataclass(frozen=True)
class RepairFlagChange:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    enabled: bool


@dataclass(frozen=True)
class WorkEntryCorrection:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    kind: Literal["work", "test", "contact", "handover", "note"]
    body: str
    reason: str


@dataclass(frozen=True)
class WorkEntryVisibility:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    deleted: bool
    reason: str


@dataclass(frozen=True)
class WorkEntryRevision:
    revision: int
    kind: Literal["work", "test", "contact", "handover", "note"]
    body: str
    editor_id: int
    editor_username: str
    reason: str
    deleted: bool
    created_at: str


@dataclass(frozen=True)
class Attachment:
    id: int
    repair_id: int
    entry_id: int | None
    filename: str
    media_type: Literal["application/octet-stream", "image/png", "image/jpeg", "image/gif", "image/webp"]
    size_bytes: int
    sha256: str
    uploader_id: int
    uploader_username: str
    created_at: str
    revision: int
    detached: bool
    previewable: bool
    detached_at: str | None = None
    purged: bool = False
    purged_at: str | None = None
    purged_by_username: str | None = None
    caption: str = ""
    entry_ids: list[int] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class AttachmentCaption:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    caption: str


@dataclass(frozen=True)
class AttachmentLink:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    entry_id: int
    linked: bool


@dataclass(frozen=True)
class AttachmentPage:
    items: list[Attachment]
    next_before: int | None


@dataclass(frozen=True)
class AttachmentDetach:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    detached: bool


@dataclass(frozen=True)
class ChargeInput:
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["part", "labor"]
    description: str
    quantity_milli: int
    unit_price_cents: int
    unit: str = ""
    unit_cost_cents: int | None = None
    notes: str = ""


@dataclass(frozen=True, kw_only=True)
class Charge(ChargeInput):
    id: int
    repair_id: int
    line_total_cents: int
    revision: int
    deleted: bool
    created_by: int
    created_by_username: str
    created_at: str
    inventory_item_id: int | None = None
    movement_id: int | None = None
    consumed_quantity_milli: int | None = None


@dataclass(frozen=True, kw_only=True)
class RepairExport(Repair):
    charges: list[Charge]


@dataclass(frozen=True)
class CustomerExport:
    customer: Customer
    repairs: list[RepairExport]


@dataclass(frozen=True)
class PartConsumption:
    __pydantic_config__ = {"extra": "forbid"}

    item_id: int
    repair_revision: int
    item_revision: int
    consumed_quantity_milli: int
    billed_quantity_milli: int
    unit_price_cents: int | None = None


@dataclass(frozen=True, kw_only=True)
class ChargePatch(ChargeInput):
    revision: int
    reason: str


@dataclass(frozen=True)
class ChargeVisibility:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    deleted: bool
    reason: str


@dataclass(frozen=True)
class DiscountChange:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int
    kind: Literal["none", "amount", "percent"]
    value: int


@dataclass(frozen=True)
class ChargeTotals:
    parts_subtotal_cents: int
    labor_subtotal_cents: int
    subtotal_cents: int
    discount_kind: Literal["none", "amount", "percent"]
    discount_value: int
    discount_cents: int
    total_cents: int
    discount_revision: int
    currency: Literal["EUR"] = "EUR"


@dataclass(frozen=True, kw_only=True)
class ChargeSummary(ChargeTotals):
    items: list[Charge]


CHARGE_CONSUMPTION_FIELDS = {"inventory_item_id", "movement_id", "consumed_quantity_milli"}
SUMMARY_CONSUMPTION_FIELDS = {"items": {"__all__": CHARGE_CONSUMPTION_FIELDS}}


@dataclass(frozen=True)
class AttachmentPurge:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int


@dataclass(frozen=True)
class ItemInput:
    __pydantic_config__ = {"extra": "forbid"}

    sku: str
    description: str
    category: str = ""
    manufacturer: str = ""
    part_number: str = ""
    package: str = ""
    specifications: str = ""
    bin: str = ""
    unit: str = "pcs"
    low_stock_milli: int = 0
    purchase_cost_cents: int | None = None
    selling_price_cents: int = 0


@dataclass(frozen=True, kw_only=True)
class InventoryItem(ItemInput):
    id: int
    revision: int
    created_at: str
    updated_at: str
    archived_at: str | None
    on_hand_milli: int = 0

    @property
    def active(self) -> bool:
        return self.archived_at is None


@dataclass(frozen=True)
class InventoryPage:
    items: list[InventoryItem]
    next_before: int | None


@dataclass(frozen=True)
class Dashboard:
    open_work: list[QueueRepair]
    open_count: int
    awaiting_pickup: list[QueueRepair]
    pickup_count: int
    recent_customers: list[CustomerListRow]
    low_stock: list[InventoryItem]
    low_stock_count: int


@dataclass(frozen=True, kw_only=True)
class ItemPatch(ItemInput):
    revision: int


@dataclass(frozen=True)
class ItemArchive:
    __pydantic_config__ = {"extra": "forbid"}

    revision: int


@dataclass(frozen=True)
class StockReceipt:
    __pydantic_config__ = {"extra": "forbid"}

    quantity_milli: int
    revision: int
    reason: str = ""


@dataclass(frozen=True)
class StockAdjustment:
    __pydantic_config__ = {"extra": "forbid"}

    quantity_milli: int
    reason: str
    revision: int


@dataclass(frozen=True)
class StockReversal:
    __pydantic_config__ = {"extra": "forbid"}

    reason: str
    revision: int


@dataclass(frozen=True)
class StockMovement:
    id: int
    item_id: int
    kind: str
    quantity_milli: int
    on_hand_after_milli: int
    reason: str
    reverses_id: int | None
    reversed_by_id: int | None
    actor_username: str
    created_at: str


@dataclass(frozen=True)
class StockChange:
    item: InventoryItem
    movement: StockMovement


@dataclass(frozen=True)
class PartReturn:
    __pydantic_config__ = {"extra": "forbid"}

    repair_revision: int
    item_revision: int
    quantity_milli: int
    reason: str


@dataclass(frozen=True)
class IntakeMistake:
    __pydantic_config__ = {"extra": "forbid"}

    repair_revision: int
    item_revision: int
    charge_revision: int
    reason: str


@dataclass(frozen=True)
class PartReturnStatus:
    item_id: int
    movement_id: int
    description: str
    repair_revision: int
    item_revision: int
    charge_revision: int
    consumed_quantity_milli: int
    remaining_quantity_milli: int
    unit: str
    on_hand_milli: int
    can_return: bool
    can_correct_intake: bool


@dataclass(frozen=True)
class PartReturnResult:
    stock: StockChange
    charge: Charge
    totals: ChargeTotals
    remaining_quantity_milli: int


@dataclass(frozen=True)
class MovementPage:
    movements: list[StockMovement]
    next_before: int | None


@dataclass(frozen=True)
class StockMismatch:
    item_id: int
    sku: str
    cached_milli: int
    ledger_milli: int


@dataclass(frozen=True)
class StockIssue:
    code: str
    shop_id: int
    item_id: int | None
    movement_id: int
    charge_id: int | None = None


@dataclass(frozen=True)
class StockReconciliation:
    items_checked: int
    mismatches: list[StockMismatch]
    issues: list[StockIssue]


# Portable shop archive transfer limit (including ZIP framing).
PORTABLE_MAX_BYTES = 1 << 30


@dataclass(frozen=True)
class PortableSourceShop:
    id: int
    name: str


@dataclass(frozen=True)
class PortablePreview:
    format: str
    schema: int
    source_shop: PortableSourceShop
    counts: dict[str, int]
    attachment_files: int
    attachment_bytes: int



@dataclass(frozen=True)
class PortableImport:
    destination_shop_id: int
    revision: int
    source_shop: PortableSourceShop
    counts: dict[str, int]
    attachment_files: int


CSV_MAX_BYTES = 256 * 1024
CSV_MAX_ROWS = 100


@dataclass(frozen=True)
class CsvIssue:
    row: int
    field: str
    message: str


@dataclass(frozen=True)
class CsvAction:
    row: int
    action: str
    fields: dict[str, str]


@dataclass(frozen=True)
class CsvPreview:
    sha256: str
    preview_id: str | None
    actions: list[CsvAction]
    errors: list[CsvIssue]


@dataclass(frozen=True)
class CsvAppliedRow:
    row: int
    id: int
    revision: int


@dataclass(frozen=True)
class CsvApply:
    sha256: str
    rows: list[CsvAppliedRow]
