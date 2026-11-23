"""Catalog and opening-stock CSV validation, SKU screening and ledger publication.

Both kinds reuse the reviewed CSV boundary in ``csv_intake``. Opening stock is the
first balance of an item: it appends one attributed ``receive`` movement to an
active item without earlier movements, under the reviewed item revision.
"""

from collections import Counter
import re

from repairlib import money
from repairlib.contracts import CsvAction, CsvAppliedRow, CsvIssue, ItemInput
from repairlib.domain.csv_intake import Intake
from repairlib.domain.customers import CustomerError
from repairlib.domain.inventory import FIELD_LIMITS, normalize
from repairlib.domain.stock import REASON_LIMIT, StockError, append_movement, clean_reason
from repairlib.storage import audit, inventory, stock

DIGITS = re.compile(r"0|[1-9][0-9]{0,15}")
NUMBER_WIDTH = 16
OPENING_REASON = "Opening stock"
CATALOG_LIMITS = {
    **FIELD_LIMITS, "low_stock_milli": NUMBER_WIDTH, "purchase_cost_cents": NUMBER_WIDTH,
    "selling_price_cents": NUMBER_WIDTH, "opening_quantity_milli": NUMBER_WIDTH,
}
OPENING_LIMITS = {"sku": FIELD_LIMITS["sku"], "quantity_milli": NUMBER_WIDTH, "reason": REASON_LIMIT}


def whole(value: str, limit: int):
    """Parse an unsigned decimal integer; None if the text is not exactly that."""
    value = value.strip()
    if not DIGITS.fullmatch(value) or int(value) > limit:
        return None
    return int(value)


def opening_quantity(row: int, field: str, value: str, errors: list, *, optional: bool):
    if optional and not value.strip():
        return 0
    quantity = whole(value, money.MAX_MILLI)
    if not quantity:
        errors.append(CsvIssue(row, field, f"Enter whole milli-units from 1 to {money.MAX_MILLI}."))
        return None
    return quantity


def repeated(skus: dict[int, str], errors: list) -> None:
    counts = Counter(skus.values())
    errors.extend(CsvIssue(row, "sku", "Repeated SKU in this CSV.")
                  for row, sku in skus.items() if counts[sku] > 1)


def plan_catalog(db, shop_id, records):
    errors = []
    proposed = {}
    skus = {}
    for row, fields in records:
        before = len(errors)
        numbers = {}
        for name, limit, default in (("low_stock_milli", money.MAX_MILLI, 0),
                                     ("purchase_cost_cents", money.MAX_CENTS, None),
                                     ("selling_price_cents", money.MAX_CENTS, ...)):
            text = fields[name].strip()
            if not text and default is not ...:
                numbers[name] = default
            elif (value := whole(text, limit)) is None:
                errors.append(CsvIssue(row, name, f"Enter a whole number from 0 to {limit}."))
            else:
                numbers[name] = value
        quantity = opening_quantity(row, "opening_quantity_milli", fields["opening_quantity_milli"], errors,
                                    optional=True)
        try:
            values = normalize(ItemInput(**{name: fields[name] for name in FIELD_LIMITS},
                                         **{name: numbers.get(name, 0) for name in (
                                             "low_stock_milli", "purchase_cost_cents", "selling_price_cents")}))
        except CustomerError as error:
            errors.extend(CsvIssue(row, field, message) for field, message in error.fields.items()
                          if field in FIELD_LIMITS)
            values = None
        sku = values["sku"] if values else fields["sku"].strip()
        if sku:
            skus[row] = sku
            if inventory.sku_exists(db, shop_id, sku):
                errors.append(CsvIssue(row, "sku", "This SKU already exists in this shop, including archived "
                                                   "items. Correct the row; existing items are not merged."))
        if values is not None and len(errors) == before:
            proposed[row] = {**{name: str(value) if value is not None else "" for name, value in values.items()},
                             "opening_quantity_milli": str(quantity) if quantity else ""}
    repeated(skus, errors)
    blocked = {issue.row for issue in errors}
    return [CsvAction(row, "create", fields) for row, fields in proposed.items() if row not in blocked], errors, []


def plan_opening(db, shop_id, records):
    errors = []
    proposed = {}
    skus = {}
    revisions = []
    for row, fields in records:
        sku = fields["sku"].strip()
        quantity = opening_quantity(row, "quantity_milli", fields["quantity_milli"], errors, optional=False)
        try:
            reason = clean_reason(fields["reason"], required=False) or OPENING_REASON
        except StockError:
            errors.append(CsvIssue(row, "reason", f"Use at most {REASON_LIMIT} printable characters."))
            reason = None
        if not sku:
            errors.append(CsvIssue(row, "sku", "This field is required."))
            continue
        skus[row] = sku
        item = inventory.find_by_sku(db, shop_id, sku)
        if item is None:
            errors.append(CsvIssue(row, "sku", "No inventory item has this SKU in this shop."))
        elif not item.active:
            errors.append(CsvIssue(row, "sku", "The inventory item is archived. Restore it first."))
        elif item.on_hand_milli or stock.has_movements(db, shop_id, item.id):
            errors.append(CsvIssue(row, "sku", "The item already has stock movements. "
                                               "Use a receipt or adjustment instead."))
        elif quantity and reason:
            revisions.append([item.id, item.revision])
            proposed[row] = {"sku": sku, "item_id": str(item.id), "item_revision": str(item.revision),
                             "quantity_milli": str(quantity), "reason": reason}
    repeated(skus, errors)
    blocked = {issue.row for issue in errors}
    return [CsvAction(row, "receive", fields) for row, fields in proposed.items() if row not in blocked], \
        errors, revisions


def receive_opening(db, principal, source, token_label, item, quantity: int, reason: str):
    updated, movement = append_movement(db, principal, item, "receive", quantity, reason)
    audit.record(db, principal, source, token_label, "inventory.opening_stock", "inventory_item", item.id, {
        "movement_id": movement.id, "kind": "receive", "quantity_milli": quantity,
        "on_hand_milli": {"before": item.on_hand_milli, "after": updated.on_hand_milli}, "reason": reason,
    })
    return updated


def publish_catalog(db, principal, source, token_label, actions):
    rows = []
    for action in actions:
        values = {name: action.fields[name] for name in FIELD_LIMITS}
        for name in ("low_stock_milli", "purchase_cost_cents", "selling_price_cents"):
            values[name] = int(action.fields[name]) if action.fields[name] else None
        item = inventory.insert(db, principal.shop_id, values)
        audit.record(db, principal, source, token_label, "inventory.create", "inventory_item", item.id, values)
        if action.fields["opening_quantity_milli"]:
            item = receive_opening(db, principal, source, token_label, item,
                                   int(action.fields["opening_quantity_milli"]), OPENING_REASON)
        rows.append(CsvAppliedRow(action.row, item.id, item.revision))
    return rows


def publish_opening(db, principal, source, token_label, actions):
    rows = []
    for action in actions:
        item = inventory.find(db, principal.shop_id, int(action.fields["item_id"]))
        if item is None or item.revision != int(action.fields["item_revision"]):
            raise StockError(409, "stale_revision", "The inventory item changed. Review the latest version.")
        item = receive_opening(db, principal, source, token_label, item,
                               int(action.fields["quantity_milli"]), action.fields["reason"])
        rows.append(CsvAppliedRow(action.row, item.id, item.revision))
    return rows


CATALOG = Intake("catalog", CATALOG_LIMITS, plan_catalog, publish_catalog)
OPENING_STOCK = Intake("opening-stock", OPENING_LIMITS, plan_opening, publish_opening)
