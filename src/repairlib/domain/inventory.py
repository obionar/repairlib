"""Inventory catalog validation, permissions and atomic mutations."""

from dataclasses import replace
from pathlib import Path

from repairlib import money
from repairlib.contracts import InventoryItem, InventoryPage, ItemInput
from repairlib.domain.customers import MAX_ID, CustomerAccess, CustomerError, authorized, search_pattern
from repairlib.domain.replay import replay as replay_receipt
from repairlib.search import fold
from repairlib.storage import audit, inventory, stock

InventoryError = CustomerError
FIELD_LIMITS = {
    "sku": 80, "description": 200, "category": 80, "manufacturer": 120,
    "part_number": 120, "package": 80, "specifications": 1000, "bin": 80, "unit": 16,
}
REQUIRED = {"sku", "description", "unit"}
NUMBER_LIMITS = {
    "low_stock_milli": money.MAX_MILLI,
    "purchase_cost_cents": money.MAX_CENTS,
    "selling_price_cents": money.MAX_CENTS,
}


def normalize(details: ItemInput) -> dict:
    if not isinstance(details, ItemInput):
        raise InventoryError(422, "validation", "Supply inventory item fields.")
    values = {}
    errors = {}
    for name, limit in FIELD_LIMITS.items():
        value = getattr(details, name)
        if not isinstance(value, str):
            errors[name] = "Enter text."
            continue
        value = value.strip().replace("\r\n", "\n")
        values[name] = value
        if name in REQUIRED and not value:
            errors[name] = "This field is required."
        elif len(value) > limit:
            errors[name] = f"Use at most {limit} characters."
        elif any(not char.isprintable() and (name != "specifications" or char not in "\n\t")
                 for char in value):
            errors[name] = "Remove unsupported characters."
    for name, limit in NUMBER_LIMITS.items():
        value = getattr(details, name)
        values[name] = value
        if value is None and name == "purchase_cost_cents":
            continue
        if type(value) is not int or not 0 <= value <= limit:
            errors[name] = f"Enter a whole number from 0 to {limit}."
    if errors:
        raise InventoryError(422, "validation", "Check the inventory item fields.", errors)
    return values


def visible(item: InventoryItem, principal) -> InventoryItem:
    return replace(item, purchase_cost_cents=None) if principal.role == "viewer" else item


def may_write(workspace: Path, access: CustomerAccess) -> bool:
    with authorized(workspace, access, resource="inventory") as (_, _, _, can_write):
        return can_write


def find_item(db, shop_id: int, item_id: int) -> InventoryItem:
    if type(item_id) is int and 1 <= item_id <= MAX_ID:
        item = inventory.find(db, shop_id, item_id)
        if item is not None:
            return item
    raise InventoryError(404, "not_found", "Inventory item not found.")


def read_item(workspace: Path, access: CustomerAccess, item_id: int) -> InventoryItem:
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        return visible(find_item(db, principal.shop_id, item_id), principal)


def list_items(workspace: Path, access: CustomerAccess, *, before: int | None = None,
               limit: int = 25, include_archived: bool = False, low_stock: bool = False,
               query: str = "", category: str = "") -> InventoryPage:
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        if (type(limit) is not int or not 1 <= limit <= 100
                or (before is not None and (type(before) is not int or not 1 <= before <= MAX_ID))
                or type(include_archived) is not bool or type(low_stock) is not bool):
            raise InventoryError(422, "validation", "Invalid inventory page.")
        if len(category) > FIELD_LIMITS["category"] or not category.isprintable():
            raise InventoryError(422, "validation", "Choose a valid inventory category.")
        rows = inventory.list_page(db, principal.shop_id, before, limit + 1,
                                   include_archived=include_archived, low_stock=low_stock,
                                   pattern=search_pattern(query), category=fold(category.strip()))
    return InventoryPage([visible(item, principal) for item in rows[:limit]],
                         rows[limit - 1].id if len(rows) > limit else None)


def categories(workspace: Path, access: CustomerAccess) -> list[str]:
    with authorized(workspace, access, resource="inventory") as (db, principal, _, _):
        return inventory.categories(db, principal.shop_id)


def replay(db, principal, operation: str, key: str, payload: dict):
    return replay_receipt(db, principal, "inventory", operation, key, payload,
                          lambda values: InventoryItem(**values), InventoryError)


def check_sku(db, shop_id: int, sku: str) -> None:
    if inventory.sku_exists(db, shop_id, sku):
        raise InventoryError(409, "duplicate_sku", "This SKU already exists in the shop.",
                             {"sku": "Enter a unique SKU."})


def create_item(workspace: Path, access: CustomerAccess, details: ItemInput, *, key: str) -> InventoryItem:
    operation = "inventory.create"
    with authorized(workspace, access, write=True, resource="inventory") as (db, principal, label, _):
        values = normalize(details)
        fingerprint, previous = replay(db, principal, operation, key, values)
        if previous is not None:
            return previous
        check_sku(db, principal.shop_id, values["sku"])
        item = inventory.insert(db, principal.shop_id, values)
        inventory.save_result(db, principal, operation, key, fingerprint, item)
        audit.record(db, principal, access.source, label, operation, "inventory_item", item.id, values)
        return item


def edit_item(workspace: Path, access: CustomerAccess, item_id: int, details: ItemInput,
              *, revision: int, key: str) -> InventoryItem:
    return _change_item(workspace, access, item_id, "inventory.edit", details, revision, key)


def archive_item(workspace: Path, access: CustomerAccess, item_id: int, *, revision: int,
                 key: str) -> InventoryItem:
    return _change_item(workspace, access, item_id, "inventory.archive", None, revision, key)


def restore_item(workspace: Path, access: CustomerAccess, item_id: int, *, revision: int,
                 key: str) -> InventoryItem:
    return _change_item(workspace, access, item_id, "inventory.restore", None, revision, key)


def _change_item(workspace: Path, access: CustomerAccess, item_id: int, operation: str,
                 details: ItemInput | None, revision: int, key: str) -> InventoryItem:
    editing = operation == "inventory.edit"
    restoring = operation == "inventory.restore"
    with authorized(workspace, access, write=True, resource="inventory") as (db, principal, label, _):
        current = find_item(db, principal.shop_id, item_id)
        if type(revision) is not int or not 1 <= revision <= MAX_ID:
            raise InventoryError(422, "validation", "Supply the reviewed inventory item revision.")
        values = normalize(details) if editing else {}
        fingerprint, previous = replay(db, principal, operation, key, {
            "id": item_id, "revision": revision, "fields": values,
        })
        if previous is not None:
            return previous
        stale = InventoryError(409, "stale_revision", "The inventory item changed. Review the latest version.")
        if current.revision != revision or revision == MAX_ID:
            raise stale
        if restoring and current.active:
            raise InventoryError(409, "same_state", "The inventory item is already active.")
        if not restoring and not current.active:
            raise InventoryError(409, "archived", "The inventory item is archived.")
        if editing:
            changes = {name: {"before": getattr(current, name), "after": value}
                       for name, value in values.items() if getattr(current, name) != value}
            if not changes:
                raise InventoryError(422, "validation", "Change at least one inventory item field.")
            if "sku" in changes:
                check_sku(db, principal.shop_id, values["sku"])
            if "unit" in changes and stock.has_movements(db, principal.shop_id, item_id):
                raise InventoryError(409, "unit_locked", "The stock unit is fixed once stock movements exist.",
                                     {"unit": "Keep the existing unit."})
            item = inventory.update(db, principal.shop_id, item_id, revision, values)
        else:
            item = inventory.set_archived(db, principal.shop_id, item_id, revision, archived=not restoring)
        if item is None:
            raise stale
        if not editing:
            changes = {"archived_at": {"before": current.archived_at, "after": item.archived_at}}
        inventory.save_result(db, principal, operation, key, fingerprint, item)
        audit.record(db, principal, access.source, label, operation, "inventory_item", item.id, changes)
        return item
