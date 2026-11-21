"""Shop-scoped inventory catalog records and stored mutation results."""

from dataclasses import fields

from repairlib.contracts import InventoryItem, ItemInput
from repairlib.storage import request_ledger

INPUT_COLUMNS = tuple(field.name for field in fields(ItemInput))
COLUMNS = ", ".join(field.name for field in fields(InventoryItem))
LOW_STOCK = "archived_at IS NULL AND on_hand_milli <= low_stock_milli"


def find(db, shop_id: int, item_id: int) -> InventoryItem | None:
    row = db.execute(f"SELECT {COLUMNS} FROM inventory_items WHERE shop_id = ? AND id = ?",
                     (shop_id, item_id)).fetchone()
    return InventoryItem(**row) if row is not None else None


def list_page(db, shop_id: int, before: int | None, limit: int,
              *, include_archived: bool = False, low_stock: bool = False,
              pattern: str | None = None, category: str = "") -> list[InventoryItem]:
    filters = ["shop_id = ?", "(? IS NULL OR id < ?)"]
    params = [shop_id, before, before]
    if low_stock:
        filters.append(LOW_STOCK)
    elif not include_archived:
        filters.append("archived_at IS NULL")
    if pattern is not None:
        filters.append(r"rl_fold(sku || ' ' || description || ' ' || manufacturer || ' ' || part_number) LIKE ? ESCAPE '\'")
        params.append(pattern)
    if category:
        filters.append("rl_fold(category) = ?")
        params.append(category)
    rows = db.execute(
        f"""
        SELECT {COLUMNS} FROM inventory_items
        WHERE {" AND ".join(filters)}
        ORDER BY id DESC LIMIT ?
        """, (*params, limit),
    )
    return [InventoryItem(**row) for row in rows]


def categories(db, shop_id: int) -> list[str]:
    return [row[0] for row in db.execute(
        """SELECT MIN(category) FROM inventory_items
        WHERE shop_id = ? AND category <> ''
        GROUP BY rl_fold(category) ORDER BY rl_fold(category) LIMIT 100""", (shop_id,),
    )]


def low_stock_preview(db, shop_id: int, limit: int) -> tuple[list[InventoryItem], int]:
    rows = list_page(db, shop_id, None, limit, low_stock=True)
    count = db.execute(f"SELECT COUNT(*) FROM inventory_items WHERE shop_id = ? AND {LOW_STOCK}",
                       (shop_id,)).fetchone()[0]
    return rows, count


def sku_exists(db, shop_id: int, sku: str) -> bool:
    return db.execute("SELECT 1 FROM inventory_items WHERE shop_id = ? AND sku = ?",
                      (shop_id, sku)).fetchone() is not None


def find_by_sku(db, shop_id: int, sku: str) -> InventoryItem | None:
    row = db.execute(f"SELECT {COLUMNS} FROM inventory_items WHERE shop_id = ? AND sku = ?",
                     (shop_id, sku)).fetchone()
    return InventoryItem(**row) if row is not None else None


def insert(db, shop_id: int, values: dict) -> InventoryItem:
    placeholders = ", ".join("?" for _ in INPUT_COLUMNS)
    row = db.execute(
        f"INSERT INTO inventory_items (shop_id, {', '.join(INPUT_COLUMNS)}) "
        f"VALUES (?, {placeholders}) RETURNING {COLUMNS}",
        (shop_id, *(values[name] for name in INPUT_COLUMNS)),
    ).fetchone()
    return InventoryItem(**row)


def update(db, shop_id: int, item_id: int, revision: int, values: dict) -> InventoryItem | None:
    assignments = ", ".join(f"{name} = ?" for name in INPUT_COLUMNS)
    row = db.execute(
        f"""
        UPDATE inventory_items SET {assignments}, revision = revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE shop_id = ? AND id = ? AND revision = ? AND archived_at IS NULL
        RETURNING {COLUMNS}
        """, (*(values[name] for name in INPUT_COLUMNS), shop_id, item_id, revision),
    ).fetchone()
    return InventoryItem(**row) if row is not None else None


def set_archived(db, shop_id: int, item_id: int, revision: int, *, archived: bool) -> InventoryItem | None:
    row = db.execute(
        f"""
        UPDATE inventory_items
        SET archived_at = CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') ELSE NULL END,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? AND (archived_at IS NULL) = ?
        RETURNING {COLUMNS}
        """, (archived, shop_id, item_id, revision, archived),
    ).fetchone()
    return InventoryItem(**row) if row is not None else None


def save_result(db, principal, operation: str, key: str, fingerprint: str,
                item: InventoryItem) -> None:
    request_ledger.store(db, "inventory", principal.shop_id, principal.user_id, operation, key,
                         fingerprint, item, link=item.id)
