"""Shop-scoped repair charges, repair discounts and retry results."""

from dataclasses import replace

from repairlib.contracts import Charge
from repairlib.money import line_total
from repairlib.storage import request_ledger

COLUMNS = ("id, repair_id, kind, description, quantity_milli, unit, unit_price_cents, "
           "unit_cost_cents, notes, revision, deleted_at IS NOT NULL AS deleted, created_by, "
           "created_by_username, created_at")
FIELDS = ("kind", "description", "quantity_milli", "unit", "unit_price_cents", "unit_cost_cents", "notes")
WITH_CONSUMPTION = f"""
    SELECT c.*, m.item_id AS inventory_item_id, m.id AS movement_id,
           -m.quantity_milli AS consumed_quantity_milli
    FROM (SELECT shop_id, {COLUMNS} FROM repair_charges) AS c
    LEFT JOIN repair_part_consumptions AS p ON p.shop_id = c.shop_id AND p.charge_id = c.id
    LEFT JOIN inventory_movements AS m ON m.shop_id = p.shop_id AND m.id = p.movement_id
"""


def record(row) -> Charge:
    values = dict(row)
    values.pop("shop_id", None)
    values["deleted"] = bool(values["deleted"])
    values["line_total_cents"] = line_total(values["quantity_milli"], values["unit_price_cents"])
    return Charge(**values)


def list_for_repair(db, shop_id: int, repair_id: int) -> list[Charge]:
    rows = db.execute(f"{WITH_CONSUMPTION} WHERE c.shop_id = ? AND c.repair_id = ? ORDER BY c.id",
                      (shop_id, repair_id))
    return [record(row) for row in rows]


def list_active_for_repairs(db, shop_id: int, repair_ids: tuple[int, ...]) -> list[Charge]:
    """One bulk read for list output; per-line rounding stays in the shared money helper."""
    if not repair_ids:
        return []
    placeholders = ", ".join("?" for _ in repair_ids)
    rows = db.execute(
        f"""SELECT {COLUMNS} FROM repair_charges
        WHERE shop_id = ? AND repair_id IN ({placeholders}) AND deleted_at IS NULL
        ORDER BY repair_id, id""",
        (shop_id, *repair_ids),
    )
    return [record(row) for row in rows]


def active_count(db, shop_id: int, repair_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM repair_charges WHERE shop_id = ? AND repair_id = ? "
                      "AND deleted_at IS NULL", (shop_id, repair_id)).fetchone()[0]


def manual_part_ids(db, shop_id: int, repair_id: int) -> set[int]:
    rows = db.execute("""SELECT c.id FROM repair_charges c
        WHERE c.shop_id = ? AND c.repair_id = ? AND c.kind = 'part' AND c.deleted_at IS NULL
          AND EXISTS (SELECT 1 FROM audit_events e
            WHERE e.shop_id = c.shop_id AND e.entity_type = 'charge' AND e.entity_id = c.id
              AND e.operation = 'charge.create' AND json_type(e.changes, '$.work_entry_id') = 'integer')""",
                      (shop_id, repair_id))
    return {row[0] for row in rows}


def find(db, shop_id: int, repair_id: int, charge_id: int) -> Charge | None:
    row = db.execute(f"{WITH_CONSUMPTION} WHERE c.shop_id = ? AND c.repair_id = ? AND c.id = ?",
                     (shop_id, repair_id, charge_id)).fetchone()
    return record(row) if row is not None else None


def insert(db, principal, repair_id: int, values: dict) -> Charge:
    row = db.execute(
        f"""INSERT INTO repair_charges(shop_id, repair_id, {', '.join(FIELDS)}, created_by,
                                       created_by_username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {COLUMNS}""",
        (principal.shop_id, repair_id, *(values[name] for name in FIELDS),
         principal.user_id, principal.username),
    ).fetchone()
    return record(row)


def link_consumption(db, shop_id: int, charge_id: int, movement_id: int) -> None:
    db.execute("INSERT INTO repair_part_consumptions(shop_id, charge_id, movement_id) VALUES (?, ?, ?)",
               (shop_id, charge_id, movement_id))


def update(db, shop_id: int, current: Charge, values: dict) -> Charge | None:
    row = db.execute(
        f"""UPDATE repair_charges SET {', '.join(f'{name} = ?' for name in FIELDS)},
            revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ? AND deleted_at IS NULL
        RETURNING {COLUMNS}""",
        (*(values[name] for name in FIELDS), shop_id, current.repair_id, current.id, current.revision),
    ).fetchone()
    return with_consumption(row, current)


def set_deleted(db, shop_id: int, current: Charge, timestamp: str | None) -> Charge | None:
    row = db.execute(
        f"""UPDATE repair_charges SET deleted_at = ?, revision = revision + 1
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ? RETURNING {COLUMNS}""",
        (timestamp, shop_id, current.repair_id, current.id, current.revision),
    ).fetchone()
    return with_consumption(row, current)


def with_consumption(row, current: Charge) -> Charge | None:
    if row is None:
        return None
    return replace(record(row), inventory_item_id=current.inventory_item_id,
                   movement_id=current.movement_id, consumed_quantity_milli=current.consumed_quantity_milli)


def discount(db, shop_id: int, repair_id: int) -> tuple[str, int, int]:
    row = db.execute("SELECT kind, value, revision FROM repair_discounts WHERE shop_id = ? AND repair_id = ?",
                     (shop_id, repair_id)).fetchone()
    return tuple(row) if row is not None else ("none", 0, 0)


def discounts_for_repairs(db, shop_id: int, repair_ids: tuple[int, ...]) -> dict[int, tuple[str, int, int]]:
    if not repair_ids:
        return {}
    placeholders = ", ".join("?" for _ in repair_ids)
    rows = db.execute(
        f"""SELECT repair_id, kind, value, revision FROM repair_discounts
        WHERE shop_id = ? AND repair_id IN ({placeholders})""",
        (shop_id, *repair_ids),
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def save_discount(db, shop_id: int, repair_id: int, kind: str, value: int, revision: int) -> bool:
    if revision == 0:
        cursor = db.execute(
            "INSERT OR IGNORE INTO repair_discounts(shop_id, repair_id, kind, value, revision) "
            "VALUES (?, ?, ?, ?, 1)", (shop_id, repair_id, kind, value))
    else:
        cursor = db.execute(
            "UPDATE repair_discounts SET kind = ?, value = ?, revision = revision + 1 "
            "WHERE shop_id = ? AND repair_id = ? AND revision = ?",
            (kind, value, shop_id, repair_id, revision))
    return cursor.rowcount == 1


def save_result(db, shop_id: int, actor_id: int, operation: str, key: str, fingerprint: str,
                repair_id: int, result) -> None:
    request_ledger.store(db, "charge", shop_id, actor_id, operation, key, fingerprint, result,
                         link=repair_id)
