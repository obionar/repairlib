#!/usr/bin/env python3
"""Seed an initialized, empty development workspace while its service is stopped.

Every field the shop can hold is filled from `demo-seed.json`: customer contact details,
technician and warranty on the ticket, work entries of each kind, catalog parts consumed from
stock, labour charges, fees, a discount, payment and pickup. Amounts are integer cents and
quantities are milli-units, exactly as the app stores them.
"""

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repairlib.client import local
from repairlib.contracts import (ChargeInput, CustomerInput, DiscountChange, ItemInput, PartConsumption,
                                 RepairFlagChange, RepairInput, StageChange, UserInput, WorkEntryInput)
from repairlib.domain import charges, customers, inventory, repairs, stock, users
from repairlib.lifecycle.workspace import WorkspaceError, database_path, maintenance_lock, workspace_status
from repairlib.storage.workspace_records import workspace_counts

# Fictional catalog so the ticket's part search finds stock: (sku, description, category, price cents, units).
CATALOG = [
    ("DEMO-USBC-PORT", "USB-C charging port", "Connectors", 1250, 6),
    ("DEMO-BAT-45", "Laptop battery 45 Wh", "Batteries", 5900, 3),
    ("DEMO-SCR-61", "Phone screen 6.1 in", "Screens", 7400, 2),
    ("DEMO-PASTE", "Thermal paste 1 g", "Consumables", 300, 20),
    ("DEMO-KB-UK", "Laptop keyboard UK layout", "Input", 3200, 1),
    ("DEMO-FAN-12", "Laptop fan 12 V", "Cooling", 2200, 4),
    ("DEMO-FUSER-KIT", "Printer fuser kit", "Printers", 8900, 2),
    ("DEMO-HINGE-KIT", "Laptop hinge and display cable kit", "Chassis", 3400, 2),
    ("DEMO-SSD-1TB", "NVMe SSD 1 TB", "Storage", 8900, 3),
]

# The named technician a seeded ticket can be assigned to; the password is for this demo only.
TECHNICIAN = ("technician", "demo-technician-passphrase")

# A stage reached straight from `new` skips the working stage; anything later passes through it.
STAGE_ORDER = {"new": (), "in_progress": ("in_progress",), "waiting_parts": ("in_progress", "waiting_parts"),
               "lab_test": ("in_progress", "lab_test"), "customer_test": ("in_progress", "lab_test", "customer_test"),
               "repaired": ("in_progress", "lab_test", "repaired"), "no_repair": ("in_progress", "no_repair")}


def current_repair(workspace, access, repair_id):
    """Read one repair after a write; the shared reader also returns its relationships."""
    return local.call(repairs.read_repair, workspace, access, repair_id)[0]


def milli(value) -> int:
    """A quantity in units as integer milli-units."""
    try:
        return int((Decimal(str(value)) * 1000).to_integral_value())
    except InvalidOperation as error:
        raise SystemExit(f"Not a quantity: {value!r}") from error


def seed(workspace, dataset):
    if workspace_status(workspace) != "initialized":
        raise SystemExit("Initialize the workspace with repairlib setup first.")
    try:
        with maintenance_lock(workspace, exclusive=True, create=False):
            seed_stopped(workspace, dataset)
    except WorkspaceError as error:
        raise SystemExit(str(error)) from error


def seed_stopped(workspace, dataset):
    report = workspace_counts(database_path(workspace), stopped=True)
    if report["customers"] or report["repairs"]:
        raise SystemExit("Refusing to seed: use an initialized workspace with no customers or repairs.")
    # The seeder already holds the exclusive lock; the local adapter would try to relock it.
    access = customers.CustomerAccess("cli")
    local.call(users.ensure_local_actor, workspace, "cli")
    technician = local.call(users.create_user, workspace, access,
                            UserInput(TECHNICIAN[0], "technician", TECHNICIAN[1]), key="seed-technician")

    items = {}
    for sku, description, category, price, units in CATALOG:
        item = local.call(inventory.create_item, workspace, access,
                          ItemInput(sku, description, category=category, selling_price_cents=price,
                                    low_stock_milli=1000), key=f"seed-item-{sku}")
        items[sku] = local.call(stock.receive_stock, workspace, access, item.id, units * 1000,
                                revision=item.revision, key=f"seed-stock-{sku}").item

    charges_made = 0
    for i, group in enumerate(dataset, 1):
        customer = local.call(customers.create_customer, workspace, access,
                              local.parse(CustomerInput, {key: group.get(key, "") for key in
                                                          ("name", "phone", "email", "address", "notes")}),
                              key=f"seed-customer-{i}")
        for j, row in enumerate(group["repairs"], 1):
            charges_made += seed_repair(workspace, access, customer, technician, items, row,
                                        f"seed-repair-{i}-{j}")
    result = workspace_counts(database_path(workspace), stopped=True)
    repair_rows = [row for group in dataset for row in group["repairs"]]
    expected = len(dataset), len(repair_rows)
    actual = result["customers"], result["repairs"]
    if actual != expected:
        raise SystemExit(f"Seed counts {actual} did not match expected {expected}.")
    print(f"Seeded {actual[0]} fictional customers, {actual[1]} repairs, {len(CATALOG)} catalog parts "
          f"and {charges_made} charges.")


def seed_repair(workspace, access, customer, technician, items, row, key_prefix) -> int:
    details = dict(row)
    stage = details.pop("stage")
    technician_name = details.pop("technician", "")
    work = details.pop("work", [])
    parts = details.pop("parts", [])
    labor = details.pop("labor", [])
    fee = details.pop("fee_cents", 0)
    discount = details.pop("discount", None)
    paid = details.pop("paid", False)
    picked_up = details.pop("picked_up", False)
    if technician_name:
        details["technician_id"] = technician.id
    # An empty warranty date must stay absent, not become an empty string.
    details = {key: value for key, value in details.items() if value != ""}
    repair = local.call(repairs.create_repair, workspace, access,
                        local.parse(RepairInput, dict(details, customer_id=customer.id)), key=key_prefix)
    if isinstance(work, str):
        work = [{"kind": "work", "body": work}] if work else []
    for index, entry in enumerate(work, 1):
        local.call(repairs.create_work_entry, workspace, access, repair.id,
                   local.parse(WorkEntryInput, entry), key=f"{key_prefix}-entry-{index}")
    if work:
        repair = current_repair(workspace, access, repair.id)
    for target in STAGE_ORDER[stage]:
        if target != repair.stage:
            repair = local.call(repairs.change_stage, workspace, access, repair.id,
                                local.parse(StageChange, {"revision": repair.revision, "stage": target}),
                                key=f"{key_prefix}-stage-{target}")
    made = 0
    for part in parts:
        item = items[part["sku"]]
        consumed, billed = milli(part["quantity"]), milli(part.get("billed", part["quantity"]))
        repair_item = local.call(inventory.read_item, workspace, access, item.id)
        local.call(stock.consume_for_repair, workspace, access, repair.id,
                   PartConsumption(item_id=item.id, repair_revision=repair.revision,
                                   item_revision=repair_item.revision, consumed_quantity_milli=consumed,
                                   billed_quantity_milli=billed, unit_price_cents=repair_item.selling_price_cents),
                   key=f"{key_prefix}-part-{part['sku']}")
        repair = current_repair(workspace, access, repair.id)
        made += 1
    for index, charge in enumerate(labor, 1):
        local.call(charges.create_charge, workspace, access, repair.id,
                   local.parse(ChargeInput, {"kind": "labor", "description": charge["description"],
                                             "quantity_milli": milli(charge.get("quantity", 1)),
                                             "unit_price_cents": charge["unit_price_cents"],
                                             "notes": charge.get("notes", "")}),
                   key=f"{key_prefix}-labor-{index}")
        repair = current_repair(workspace, access, repair.id)
        made += 1
    if fee:
        local.call(charges.set_fees, workspace, access, repair.id, labor=fee, discount=None,
                   revision=repair.revision, key=f"{key_prefix}-fees")
        repair = current_repair(workspace, access, repair.id)
        made += 1
    if discount:
        local.call(charges.set_discount, workspace, access, repair.id,
                   local.parse(DiscountChange, {"revision": 0, **discount}), key=f"{key_prefix}-discount")
        repair = current_repair(workspace, access, repair.id)
    if paid:
        repair = local.call(repairs.change_repair_flag, workspace, access, repair.id,
                            RepairFlagChange(repair.revision, True), flag="payment", key=f"{key_prefix}-paid")
    if picked_up:
        repair = local.call(repairs.change_repair_flag, workspace, access, repair.id,
                            RepairFlagChange(repair.revision, True), flag="pickup", key=f"{key_prefix}-pickup")
    return made


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("demo-seed.json"))
    args = parser.parse_args()
    seed(Path(args.workspace).expanduser().resolve(), json.loads(args.dataset.read_text(encoding="utf-8")))
