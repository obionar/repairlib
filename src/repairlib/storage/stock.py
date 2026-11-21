"""Append-only stock movements, the cached on-hand quantity and stored retry results."""


from repairlib.contracts import InventoryItem, StockMismatch, StockMovement
from repairlib.storage.inventory import COLUMNS
from repairlib.storage import request_ledger

MOVEMENT = """
    SELECT m.id, m.item_id, m.kind, m.quantity_milli, m.on_hand_after_milli, m.reason,
           m.reverses_id, r.id AS reversed_by_id, u.username AS actor_username, m.created_at
    FROM inventory_movements AS m
    JOIN users AS u ON u.shop_id = m.shop_id AND u.id = m.actor_user_id
    LEFT JOIN inventory_movements AS r ON r.shop_id = m.shop_id AND r.reverses_id = m.id AND r.kind = 'reversal'
"""


def find_movement(db, shop_id: int, item_id: int, movement_id: int) -> StockMovement | None:
    row = db.execute(f"{MOVEMENT} WHERE m.shop_id = ? AND m.item_id = ? AND m.id = ?",
                     (shop_id, item_id, movement_id)).fetchone()
    return StockMovement(**row) if row is not None else None


def has_movements(db, shop_id: int, item_id: int) -> bool:
    return db.execute("SELECT 1 FROM inventory_movements WHERE shop_id = ? AND item_id = ? LIMIT 1",
                      (shop_id, item_id)).fetchone() is not None


def returned_quantity(db, shop_id: int, movement_id: int) -> int:
    return db.execute(
        """SELECT COALESCE(SUM(quantity_milli), 0) FROM inventory_movements
        WHERE shop_id = ? AND reverses_id = ? AND kind = 'return'""",
        (shop_id, movement_id),
    ).fetchone()[0]


def movement_page(db, shop_id: int, item_id: int, before: int | None, limit: int) -> list[StockMovement]:
    rows = db.execute(
        f"{MOVEMENT} WHERE m.shop_id = ? AND m.item_id = ? AND (? IS NULL OR m.id < ?) "
        "ORDER BY m.id DESC LIMIT ?", (shop_id, item_id, before, before, limit))
    return [StockMovement(**row) for row in rows]


def append(db, principal, item: InventoryItem, kind: str, quantity: int, reason: str,
           reverses_id: int | None) -> tuple[InventoryItem, StockMovement] | None:
    """Apply one delta under the reviewed revision; None means the row changed."""
    row = db.execute(
        f"""
        UPDATE inventory_items SET on_hand_milli = on_hand_milli + ?, revision = revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE shop_id = ? AND id = ? AND revision = ? AND archived_at IS NULL
        RETURNING {COLUMNS}
        """, (quantity, principal.shop_id, item.id, item.revision),
    ).fetchone()
    if row is None:
        return None
    updated = InventoryItem(**row)
    (movement_id,) = db.execute(
        """
        INSERT INTO inventory_movements
            (shop_id, item_id, kind, quantity_milli, on_hand_after_milli, reason, reverses_id, actor_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """, (principal.shop_id, item.id, kind, quantity, updated.on_hand_milli, reason, reverses_id,
              principal.user_id),
    ).fetchone()
    return updated, find_movement(db, principal.shop_id, item.id, movement_id)


def mismatches(db, shop_id: int | None) -> tuple[int, list[StockMismatch]]:
    """Compare each item's cached on-hand with the sum of its ledger (all shops if None)."""
    rows = db.execute(
        """
        SELECT i.id AS item_id, i.sku, i.on_hand_milli AS cached_milli,
               COALESCE(SUM(m.quantity_milli), 0) AS ledger_milli
        FROM inventory_items AS i
        LEFT JOIN inventory_movements AS m ON m.shop_id = i.shop_id AND m.item_id = i.id
        WHERE ? IS NULL OR i.shop_id = ?
        GROUP BY i.shop_id, i.id ORDER BY i.shop_id, i.id
        """, (shop_id, shop_id),
    ).fetchall()
    return len(rows), [StockMismatch(**row) for row in rows if row["cached_milli"] != row["ledger_milli"]]


def save_result(db, principal, operation: str, key: str, fingerprint: str,
                movement_id: int, result) -> None:
    request_ledger.store(db, "stock", principal.shop_id, principal.user_id, operation, key,
                         fingerprint, result, link=movement_id)
