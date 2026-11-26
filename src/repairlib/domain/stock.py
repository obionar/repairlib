"""Stock receipts, adjustments, reversals, repair consumption and returns over one ledger.

The movement ledger is authoritative. Each write checks the reviewed item revision,
appends one movement, updates the cached on-hand quantity, stores the retry result and
writes the attributed audit row in a single BEGIN IMMEDIATE transaction.
"""

from dataclasses import asdict, fields, replace
from pathlib import Path

from repairlib import money
from repairlib.contracts import (
    Charge, ChargeInput, ChargeTotals, IntakeMistake, InventoryItem, MovementPage, PartConsumption, PartReturn,
    PartReturnResult, PartReturnStatus, StockChange, StockMovement, StockReconciliation,
)
from repairlib.domain import charges as billing
from repairlib.domain.customers import MAX_ID, CustomerAccess, CustomerError, authorized
from repairlib.domain.replay import replay as replay_receipt
from repairlib.domain.inventory import find_item
from repairlib.domain.repairs import find_repair, normalize_entry
from repairlib.storage import audit, charges, repairs, stock, stock_reconciliation
from repairlib.storage.initialization import SCHEMA_VERSION

StockError = CustomerError
MAX_ON_HAND = 1000 * money.MAX_MILLI
REASON_LIMIT = 200


def clean_reason(reason, *, required: bool) -> str:
    if not isinstance(reason, str):
        raise StockError(422, "validation", "Enter a reason.", {"reason": "Enter text."})
    value = reason.strip()
    if required and not value:
        raise StockError(422, "validation", "Enter a reason.", {"reason": "This field is required."})
    if len(value) > REASON_LIMIT or any(not char.isprintable() for char in value):
        raise StockError(422, "validation", "Check the reason.",
                         {"reason": f"Use at most {REASON_LIMIT} printable characters."})
    return value


def check_quantity(quantity, *, signed: bool, field: str = "quantity_milli") -> int:
    low = -money.MAX_MILLI if signed else 1
    if type(quantity) is not int or not low <= quantity <= money.MAX_MILLI or quantity == 0:
        raise StockError(422, "validation", "Enter a non-zero quantity within limits.",
                         {field: "Enter a valid quantity."})
    return quantity


def stock_change_shape(result: dict) -> StockChange:
    return StockChange(InventoryItem(**result["item"]), StockMovement(**result["movement"]))


def part_return_shape(result: dict) -> PartReturnResult:
    return PartReturnResult(stock_change_shape(result["stock"]), billing.charge_shape(result["charge"]),
                            ChargeTotals(**result["totals"]), result["remaining_quantity_milli"])


def replay(db, principal, operation: str, key: str, payload: dict, shape=stock_change_shape):
    return replay_receipt(db, principal, "stock", operation, key, payload, shape, StockError)


def consume_for_repair(workspace: Path, access: CustomerAccess, repair_id: int, use: PartConsumption,
                       *, key: str, work_note: str = "") -> Charge:
    operation = "inventory.consume"
    with authorized(workspace, access, write=True, resource="repairs") as (db, principal, label, _):
        repair = find_repair(db, principal.shop_id, repair_id)
        item = find_item(db, principal.shop_id, use.item_id)
        validate_review(use.repair_revision, use.item_revision)
        payload = {"repair_id": repair.id, **asdict(use)}
        work_note = work_note.strip().replace("\r\n", "\n")
        if work_note:
            payload["work_note"] = work_note
        fingerprint, previous = replay(db, principal, operation, key, payload, billing.charge_shape)
        if previous is not None:
            return previous
        check_review(repair, item, use.repair_revision, use.item_revision)
        quantity = check_quantity(use.consumed_quantity_milli, signed=False, field="consumed_quantity_milli")
        billed = check_quantity(use.billed_quantity_milli, signed=False, field="billed_quantity_milli")
        if quantity > item.on_hand_milli:
            raise StockError(409, "insufficient_stock", "Not enough stock on hand for this change.",
                             {"consumed_quantity_milli": "Stock cannot go below zero."})
        values = billing.normalize(ChargeInput(
            kind="part", description=item.description, unit=item.unit,
            unit_cost_cents=item.purchase_cost_cents, quantity_milli=billed,
            unit_price_cents=(item.selling_price_cents if use.unit_price_cents is None else use.unit_price_cents),
        ))
        updated, movement = append_movement(db, principal, item, "consume", -quantity, "")
        charge = billing.append_charge(db, principal, repair.id, values)
        billing.build_summary(db, principal, repair.id)
        charges.link_consumption(db, principal.shop_id, charge.id, movement.id)
        heading = billing.part_used(billing.shop_text(db, principal.shop_id), item.description, quantity, item.unit)
        kind, body = normalize_entry("work", f"{heading}\n{work_note}" if work_note else heading)
        entry = repairs.insert_entry(db, principal, repair.id, kind, body)
        charge = replace(charge, inventory_item_id=item.id, movement_id=movement.id,
                         consumed_quantity_milli=quantity)
        stock.save_result(db, principal, operation, key, fingerprint, movement.id, charge)
        audit.record(db, principal, access.source, label, operation, "charge", charge.id, {
            "repair_id": repair.id, "item_id": item.id, "movement_id": movement.id, "work_entry_id": entry.id,
            "on_hand_milli": {"before": item.on_hand_milli, "after": updated.on_hand_milli},
            "consumed_quantity_milli": quantity, "billed_quantity_milli": charge.quantity_milli,
            "unit_price_cents": charge.unit_price_cents,
        })
        return charge


def validate_review(repair_revision: int, item_revision: int) -> None:
    if any(type(value) is not int or not 1 <= value <= MAX_ID for value in (repair_revision, item_revision)):
        raise StockError(422, "validation", "Supply the reviewed repair and inventory item revisions.")


def check_review(repair, item, repair_revision: int, item_revision: int) -> None:
    if repair.archived_at is not None:
        raise StockError(409, "archived", "Restore the archived repair before changing it.")
    if not item.active:
        raise StockError(409, "archived", "The inventory item is archived.")
    if repair.revision != repair_revision:
        raise StockError(409, "stale_revision", "The repair changed. Review the latest version.")
    if item.revision != item_revision or item.revision == MAX_ID:
        raise stale_item()


def stale_item() -> StockError:
    return StockError(409, "stale_revision", "The inventory item changed. Review the latest version.")


def append_movement(db, principal, item, kind, quantity, reason, original_id=None):
    if item.on_hand_milli + quantity > MAX_ON_HAND:
        raise StockError(422, "validation", "Stock on hand would exceed the supported limit.",
                         {"quantity_milli": "Enter a smaller quantity."})
    applied = stock.append(db, principal, item, kind, quantity, reason, original_id)
    if applied is None:
        raise stale_item()
    return applied


def linked_part(db, shop_id: int, repair_id: int, charge_id: int):
    charge = billing.find_charge(db, shop_id, repair_id, charge_id)
    if charge.movement_id is None:
        raise StockError(409, "not_inventory_linked", "This charge has no inventory consumption.")
    return charge, find_item(db, shop_id, charge.inventory_item_id)


def remaining_use(db, shop_id: int, charge: Charge) -> int:
    return charge.consumed_quantity_milli - stock.returned_quantity(db, shop_id, charge.movement_id)


def part_return_status(workspace: Path, access: CustomerAccess, repair_id: int,
                       charge_id: int) -> PartReturnStatus:
    with authorized(workspace, access, resource="repairs") as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        charge, item = linked_part(db, principal.shop_id, repair.id, charge_id)
        remaining = remaining_use(db, principal.shop_id, charge)
        can_return = can_write and repair.archived_at is None and item.active and remaining > 0
        return PartReturnStatus(
            item.id, charge.movement_id, charge.description, repair.revision, item.revision, charge.revision,
            charge.consumed_quantity_milli, remaining, item.unit, item.on_hand_milli,
            can_return, can_return and not charge.deleted,
        )


def return_part(workspace: Path, access: CustomerAccess, repair_id: int, charge_id: int,
                change: PartReturn, *, key: str) -> PartReturnResult:
    quantity = check_quantity(change.quantity_milli, signed=False)
    return _return_part(workspace, access, repair_id, charge_id, change, key, quantity=quantity)


def correct_intake_mistake(workspace: Path, access: CustomerAccess, repair_id: int, charge_id: int,
                           change: IntakeMistake, *, key: str) -> PartReturnResult:
    billing.validate_charge_revision(change.charge_revision)
    return _return_part(workspace, access, repair_id, charge_id, change, key, quantity=None)


def _return_part(workspace, access, repair_id, charge_id, change, key, *, quantity):
    combined = quantity is None
    operation = "inventory.intake_mistake" if combined else "inventory.return"
    validate_review(change.repair_revision, change.item_revision)
    reason = clean_reason(change.reason, required=True)
    with authorized(workspace, access, write=True, resource="repairs") as (db, principal, label, _):
        repair = find_repair(db, principal.shop_id, repair_id)
        charge, item = linked_part(db, principal.shop_id, repair.id, charge_id)
        fingerprint, previous = replay(db, principal, operation, key, {
            **asdict(change), "reason": reason, "repair_id": repair.id, "charge_id": charge.id,
        }, part_return_shape)
        if previous is not None:
            return previous
        check_review(repair, item, change.repair_revision, change.item_revision)
        remaining = remaining_use(db, principal.shop_id, charge)
        returned = remaining if combined else quantity
        if not 1 <= returned <= remaining:
            raise StockError(409, "return_exceeds_consumption", "Return quantity exceeds the remaining consumption.",
                             {"quantity_milli": "Use a quantity within the remaining consumption."})
        before = billing.build_summary(db, principal, repair.id)
        applied = StockChange(*append_movement(
            db, principal, item, "return", returned, reason, charge.movement_id))
        billed = charge.quantity_milli
        if combined:
            charge = billing.apply_charge_visibility(db, principal, charge, change.charge_revision, True)
        elif not charge.deleted:
            charge = credit_return(db, principal, charge, returned)
        after = billing.build_summary(db, principal, repair.id)
        totals = ChargeTotals(**{field.name: getattr(after, field.name) for field in fields(ChargeTotals)})
        result = PartReturnResult(applied, charge, totals, remaining - returned)
        stock.save_result(db, principal, operation, key, fingerprint, applied.movement.id, result)
        audit.record(db, principal, access.source, label, operation, "charge", charge.id, {
            "repair_id": repair.id, "item_id": item.id, "movement_id": applied.movement.id,
            "reverses_id": charge.movement_id, "quantity_milli": returned,
            "on_hand_milli": {"before": item.on_hand_milli, "after": applied.item.on_hand_milli},
            "total_cents": {"before": before.total_cents, "after": after.total_cents},
            "reason": reason, **({"deleted": {"before": False, "after": True},
                                  "charge_revision": charge.revision} if combined else {
                "billed_quantity_milli": {"before": billed, "after": 0 if charge.deleted else charge.quantity_milli}}),
        })
        return result


def credit_return(db, principal, charge: Charge, returned: int) -> Charge:
    """Take the returned quantity off the bill at the charge's own price; nothing left removes the line."""
    billed = charge.quantity_milli - returned
    if billed <= 0:
        return billing.apply_charge_visibility(db, principal, charge, charge.revision, True)
    values = {name: getattr(charge, name) for name in charges.FIELDS} | {"quantity_milli": billed}
    updated = charges.update(db, principal.shop_id, charge, values)
    if updated is None:
        raise StockError(409, "stale_revision", "The charge changed. Review the latest version.")
    return updated


def receive_stock(workspace: Path, access: CustomerAccess, item_id: int, quantity_milli: int, *,
                  revision: int, key: str, reason: str = "") -> StockChange:
    return _move(workspace, access, item_id, "inventory.receive", quantity_milli, reason, revision, key)


def adjust_stock(workspace: Path, access: CustomerAccess, item_id: int, quantity_milli: int,
                 reason: str, *, revision: int, key: str) -> StockChange:
    return _move(workspace, access, item_id, "inventory.adjust", quantity_milli, reason, revision, key)


def reverse_movement(workspace: Path, access: CustomerAccess, item_id: int, movement_id: int,
                     reason: str, *, revision: int, key: str) -> StockChange:
    return _move(workspace, access, item_id, "inventory.reverse", None, reason, revision, key,
                 movement_id=movement_id)


def _move(workspace, access, item_id, operation, quantity, reason, revision, key, *, movement_id=None):
    with authorized(workspace, access, write=True, resource="inventory") as (db, principal, label, _):
        current = find_item(db, principal.shop_id, item_id)
        if type(revision) is not int or not 1 <= revision <= MAX_ID:
            raise StockError(422, "validation", "Supply the reviewed inventory item revision.")
        reverse = operation == "inventory.reverse"
        reason = clean_reason(reason, required=operation != "inventory.receive")
        if reverse:
            if type(movement_id) is not int or not 1 <= movement_id <= MAX_ID:
                raise StockError(404, "not_found", "Stock movement not found.")
            payload = {"id": item_id, "movement": movement_id}
        else:
            quantity = check_quantity(quantity, signed=operation == "inventory.adjust")
            payload = {"id": item_id, "quantity_milli": quantity}
        fingerprint, previous = replay(db, principal, operation, key,
                                       {**payload, "revision": revision, "reason": reason})
        if previous is not None:
            return previous
        if reverse:
            original = stock.find_movement(db, principal.shop_id, item_id, movement_id)
            if original is None:
                raise StockError(404, "not_found", "Stock movement not found.")
            if original.kind == "reversal":
                raise StockError(409, "not_reversible", "A reversal cannot itself be reversed.")
            if original.kind == "consume":
                raise StockError(409, "not_reversible", "Repair consumption requires a repair return.")
            if original.kind == "return":
                raise StockError(409, "not_reversible", "A repair return cannot be reversed here.")
            if original.reversed_by_id is not None:
                raise StockError(409, "already_reversed", "This stock movement is already reversed.")
            quantity = -original.quantity_milli
        if current.revision != revision or revision == MAX_ID:
            raise stale_item()
        if not current.active:
            raise StockError(409, "archived", "The inventory item is archived.")
        after = current.on_hand_milli + quantity
        if after < 0:
            raise StockError(409, "insufficient_stock", "Not enough stock on hand for this change.",
                             {"quantity_milli": "Stock cannot go below zero."})
        kind = {"inventory.receive": "receive", "inventory.adjust": "adjust"}.get(operation, "reversal")
        change = StockChange(*append_movement(db, principal, current, kind, quantity, reason, movement_id))
        stock.save_result(db, principal, operation, key, fingerprint, change.movement.id, change)
        audit.record(db, principal, access.source, label, operation, "inventory_item", item_id, {
            "movement_id": change.movement.id, "kind": kind, "quantity_milli": quantity,
            "on_hand_milli": {"before": current.on_hand_milli, "after": change.item.on_hand_milli},
            "reason": reason, "reverses_id": movement_id,
        })
        return change


def read_movement(workspace: Path, access: CustomerAccess, item_id: int, movement_id: int) -> StockMovement:
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        movement = (stock.find_movement(db, principal.shop_id, item_id, movement_id)
                    if 1 <= item_id <= MAX_ID and 1 <= movement_id <= MAX_ID else None)
        if movement is None:
            raise StockError(404, "not_found", "Stock movement not found.")
        return movement


def list_movements(workspace: Path, access: CustomerAccess, item_id: int, *, before: int | None = None,
                   limit: int = 25) -> MovementPage:
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        find_item(db, principal.shop_id, item_id)
        if (type(limit) is not int or not 1 <= limit <= 100
                or (before is not None and (type(before) is not int or not 1 <= before <= MAX_ID))):
            raise StockError(422, "validation", "Invalid stock movement page.")
        rows = stock.movement_page(db, principal.shop_id, item_id, before, limit + 1)
    return MovementPage(rows[:limit], rows[limit - 1].id if len(rows) > limit else None)


def reconcile_stock(workspace: Path, access: CustomerAccess) -> StockReconciliation:
    """Check the authenticated shop's inventory ledger without changing state."""
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        return stock_reconciliation.reconcile(db, principal.shop_id, SCHEMA_VERSION)
