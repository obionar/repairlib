#!/usr/bin/env python3
"""Seed an initialized, empty development workspace while its service is stopped."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repairlib.client import local
from repairlib.contracts import (CustomerInput, ItemInput, RepairFlagChange, RepairInput, StageChange,
                                 WorkEntryInput)
from repairlib.domain import customers, inventory, repairs, stock, users
from repairlib.lifecycle.workspace import WorkspaceError, database_path, maintenance_lock, workspace_status
from repairlib.storage.workspace_records import workspace_counts

# Fictional catalog so the ticket's part search finds stock: (sku, description, category, price cents, units).
CATALOG = [
    ("DEMO-USBC-PORT", "USB-C charging port", "Connectors", 1250, 6),
    ("DEMO-BAT-45", "Laptop battery 45 Wh", "Batteries", 5900, 3),
    ("DEMO-SCR-61", "Phone screen 6.1 in", "Screens", 7400, 2),
    ("DEMO-PASTE", "Thermal paste 1 g", "Consumables", 300, 20),
    ("DEMO-KB-UK", "Laptop keyboard UK layout", "Input", 3200, 1),
]


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
    for i, group in enumerate(dataset, 1):
        customer = local.call(customers.create_customer, workspace, access,
                              CustomerInput(group["name"]), key=f"seed-customer-{i}")
        for j, row in enumerate(group["repairs"], 1):
            details = dict(row)
            stage = details.pop("stage")
            work = details.pop("work", "")
            picked_up = details.pop("picked_up", False)
            key = f"seed-repair-{i}-{j}"
            repair = local.call(repairs.create_repair, workspace, access,
                                local.parse(RepairInput, dict(details, customer_id=customer.id)), key=key)
            if work:
                local.call(repairs.create_work_entry, workspace, access, repair.id,
                           WorkEntryInput("work", work), key=key + "-work")
            if stage in {"customer_test", "repaired"}:
                repair = local.call(repairs.change_stage, workspace, access, repair.id,
                                    StageChange(repair.revision, "in_progress"), key=key + "-progress")
            if stage not in {"new", repair.stage}:
                repair = local.call(repairs.change_stage, workspace, access, repair.id,
                                    local.parse(StageChange, {"revision": repair.revision, "stage": stage}),
                                    key=key + "-stage")
            if picked_up:
                local.call(repairs.change_repair_flag, workspace, access, repair.id,
                           RepairFlagChange(repair.revision, True), flag="pickup", key=key + "-pickup")
    for sku, description, category, price, units in CATALOG:
        item = local.call(inventory.create_item, workspace, access,
                          ItemInput(sku, description, category=category, selling_price_cents=price,
                                    low_stock_milli=1000), key=f"seed-item-{sku}")
        local.call(stock.receive_stock, workspace, access, item.id, units * 1000,
                   revision=item.revision, key=f"seed-stock-{sku}")
    result = workspace_counts(database_path(workspace), stopped=True)
    expected = len(dataset), sum(len(group["repairs"]) for group in dataset)
    actual = result["customers"], result["repairs"]
    if actual != expected:
        raise SystemExit(f"Seed counts {actual} did not match expected {expected}.")
    print(f"Seeded {actual[0]} fictional customers, {actual[1]} repairs and {len(CATALOG)} catalog parts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("demo-seed.json"))
    args = parser.parse_args()
    seed(Path(args.workspace).expanduser().resolve(), json.loads(args.dataset.read_text(encoding="utf-8")))
