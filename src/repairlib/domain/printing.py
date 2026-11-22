"""Browser print views and shop-scoped, single-attempt P15 jobs."""

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import subprocess

from repairlib.contracts import (
    ChargeSummary, LabelOptions, PrintReceipt, PrinterChange, PrinterSettings, Repair,
    WorkEntry,
)
from repairlib.domain.charges import build_summary
from repairlib.domain import inventory
from repairlib.domain.customers import FIELD_LIMITS, MAX_ID, authorized
from repairlib.domain.replay import replay as replay_receipt
from repairlib.domain.users import authorized as admin_authorized, validate_key
from repairlib.domain.repairs import RepairAccess, RepairError, find_repair
from repairlib.paths import database_path
from repairlib.storage import printer, repairs, settings


@dataclass(frozen=True)
class PrintView:
    shop_name: str
    repair: Repair
    customer_name: str
    customer_phone: str
    technician: str
    entries: list[WorkEntry]
    entries_truncated: bool
    charges: ChargeSummary
    label_options: LabelOptions | None
    shop_address: str = ""
    shop_phone: str = ""
    terms: str = ""
    payment_paid: bool | None = None


def print_view(workspace: Path, access: RepairAccess, repair_id: int, *, label: bool = False) -> PrintView:
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        names = repairs.relationship_names(db, principal.shop_id, repair)
        phone = repairs.customer_phone(db, principal.shop_id, repair)
        limit = 200
        entries = [] if label else repairs.list_entries(db, principal.shop_id, repair_id, None, limit + 1,
                                                         live_only=True)
        summary = build_summary(db, principal, repair_id)
        printable = ChargeSummary(**(vars(summary) | {
            "items": [item for item in summary.items if not item.deleted]}))
        options = label_options(db, principal.shop_id, repair) if label and can_write else None
        shop = settings.find(db, principal.shop_id)
        return PrintView(shop.name, repair, names["customer"], phone,
                         names["technician"], list(reversed(entries[:limit])), len(entries) > limit,
                         printable, options,
                         shop_address=shop.address, shop_phone=shop.phone, terms=shop.terms,
                         payment_paid=repairs.payment_status(db, principal.shop_id, repair_id))


P15_TIMEOUT = 45
UNCERTAIN = PrintReceipt("uncertain", "The print outcome is uncertain. The label may already have reached paper. "
                                      "Check the printer before requesting another label.")
FAILED = PrintReceipt("failed", "P15 reported a failure. The label may already have reached paper. "
                                "Check the printer before requesting another label.")
ACKNOWLEDGED = PrintReceipt("acknowledged", "P15 acknowledged the label.")
UNAVAILABLE = PrintReceipt("unavailable", "The P15 program could not be started on the server.")


def p15_binary() -> str | None:
    if os.name != "posix":
        return None
    # Resolve only the fixed program name on the operator's absolute PATH entries.
    search = os.pathsep.join(path for path in os.get_exec_path() if os.path.isabs(path))
    return shutil.which("p15", path=search) if search else None


def read_printer(workspace: Path, access: RepairAccess) -> PrinterSettings:
    with admin_authorized(workspace, access, capability="view the printer configuration") as (db, principal, _):
        driver, revision = printer.configuration(db, principal.shop_id)
    return PrinterSettings(driver, revision, p15_binary() is not None)


def inventory_label_printer(workspace: Path, access: RepairAccess) -> tuple[str, int]:
    """Printer driver and revision for an inventory label; any inventory reader may see them."""
    with inventory.authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        return printer.configuration(db, principal.shop_id)


def read_label_options(workspace: Path, access: RepairAccess, repair_id: int) -> LabelOptions:
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        if not can_write:
            raise RepairError(403, "forbidden", "Your account or token cannot send labels to P15.")
        return label_options(db, principal.shop_id, repair)


def label_options(db, shop_id: int, repair: Repair) -> LabelOptions:
    driver, revision = printer.configuration(db, shop_id)
    return LabelOptions(repair.revision, revision, driver == "p15", p15_binary() is not None)


def reviewed_revision(revision: int) -> None:
    if type(revision) is not int or not 1 <= revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed revision.")


def replay(db, principal, operation: str, key: str, payload: dict):
    return replay_receipt(db, principal, "printer", operation, key, payload, dict, RepairError)


def configure_printer(workspace: Path, access: RepairAccess, change: PrinterChange, *,
                      key: str) -> PrinterSettings:
    validate_key(key)
    reviewed_revision(change.revision)
    if change.driver not in {"p15", "none"}:
        raise RepairError(422, "validation", "Choose P15 or Disabled.")
    operation = "printer.configure"
    with admin_authorized(workspace, access, write=True, capability="configure the printer") as (
        db, principal, token_label,
    ):
        fingerprint, previous = replay(db, principal, operation, key, asdict(change))
        if previous is not None:
            return PrinterSettings(**previous)
        shop_id = principal.shop_id
        driver, revision = printer.configuration(db, shop_id)
        if revision != change.revision:
            raise RepairError(409, "stale_revision", "The printer configuration changed. Review it again.")
        if driver == change.driver:
            raise RepairError(422, "validation", "Choose a different printer setting.")
        printer.configure(db, shop_id, change.driver, revision)
        result = PrinterSettings(change.driver, revision + 1, p15_binary() is not None)
        printer.reserve(db, principal, operation, key, fingerprint, None, result)
        repairs.audit(db, principal, access.source, token_label, operation, "shop_settings", shop_id,
                      {"driver": {"before": driver, "after": change.driver}})
        return result


def print_p15(workspace: Path, access: RepairAccess, *, printer_revision: int, key: str,
              repair_id: int | None = None, repair_revision: int | None = None,
              item_id: int | None = None, item_revision: int | None = None) -> PrintReceipt:
    validate_key(key)
    reviewed_revision(printer_revision)
    if item_id is not None and repair_id is not None:
        raise RepairError(422, "validation", "Choose one label.")
    operation = "printer.inventory_label" if item_id is not None else (
        "printer.test" if repair_id is None else "printer.label")
    with authorized(workspace, access, write=True, **({"resource": "inventory"} if item_id is not None else {})) as (db, principal, token_label, _):
        shop_id = principal.shop_id
        repair = None
        item = None
        if item_id is not None:
            item = inventory.find_item(db, shop_id, item_id)
            reviewed_revision(item_revision)
        elif repair_id is None:
            if principal.role != "admin":
                raise RepairError(403, "forbidden", "Only an administrator can print a test label.")
        else:
            repair = find_repair(db, shop_id, repair_id)
            reviewed_revision(repair_revision)
        payload = {
            "repair_id": repair_id, "repair_revision": repair_revision, "printer_revision": printer_revision,
        }
        if item_id is not None:
            payload.update(item_id=item_id, item_revision=item_revision)
        fingerprint, previous = replay(db, principal, operation, key, payload)
        if previous is not None:
            return PrintReceipt(**previous)
        driver, revision = printer.configuration(db, shop_id)
        if (printer_revision != revision or (repair is not None and repair_revision != repair.revision)
                or (item is not None and item_revision != item.revision)):
            raise RepairError(409, "stale_revision", "The printer configuration or repair changed. Review it again.")
        if driver != "p15":
            raise RepairError(409, "printer_disabled", "Direct label printing is disabled for this shop.")
        binary = p15_binary()
        if binary is None:
            raise RepairError(503, "printer_unavailable", "The P15 program is unavailable on the server.")
        if item is not None:
            args = [binary, "inv", "-n", "1", "--", item.sku, item.description]
            entity_type, entity_id = "inventory_item", item.id
        elif repair is None:
            args = [binary, "text", "-n", "1", "--", "RepairLib test"]
            entity_type, entity_id = "shop_settings", shop_id
        else:
            name = repairs.relationship_names(db, shop_id, repair)["customer"].strip()
            name = "".join(char for char in name[:FIELD_LIMITS["name"]] if char.isprintable())
            args = [binary, "repair", "-n", "1", "--", str(repair.id), name]
            entity_type, entity_id = "repair", repair.id
        # Commit an uncertain result before launching. A duplicate, even after
        # worker loss, can only read the receipt. No DB lock spans the driver call.
        printer.reserve(db, principal, operation, key, fingerprint, repair_id, UNCERTAIN)
        repairs.audit(db, principal, access.source, token_label, operation + ".requested",
                      entity_type, entity_id, {"driver": "p15", "outcome": UNCERTAIN.outcome})

    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=P15_TIMEOUT, check=False)
        receipt = ACKNOWLEDGED if result.returncode == 0 else FAILED
    except subprocess.TimeoutExpired:
        receipt = UNCERTAIN
    except OSError:
        receipt = UNAVAILABLE
    if not printer.complete(database_path(workspace), principal, access.source, token_label,
                            operation, key, entity_type, entity_id, receipt):
        return UNCERTAIN
    return receipt
