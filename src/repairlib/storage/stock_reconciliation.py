"""Read-only inventory checks for a consistent SQLite read snapshot."""

import json

from repairlib.contracts import StockIssue, StockReconciliation
from repairlib.storage.stock import mismatches


def reconcile(db, shop_id: int | None, version: int) -> StockReconciliation:
    """A null shop selects all shops for stopped-workspace diagnostics."""
    if version < 18:
        return StockReconciliation(0, [], [])
    scope = (shop_id, shop_id)
    checked, differences = mismatches(db, shop_id)
    issues = movement_issues(db, scope)
    if version >= 19:
        issues.extend(consumption_link_issues(db, scope))
        issues.extend(repair_snapshot_issues(db, scope, portable=version >= 22))
    return StockReconciliation(checked, differences, issues)


def movement_issues(db, scope) -> list[StockIssue]:
    rows = db.execute("""
        WITH ledger AS (
            SELECT shop_id, item_id, id, kind, quantity_milli, on_hand_after_milli, reverses_id, actor_user_id,
                SUM(quantity_milli) OVER (PARTITION BY shop_id, item_id ORDER BY id
                    ROWS UNBOUNDED PRECEDING) AS running_balance,
                COUNT(*) OVER references_by_kind AS references_count,
                SUM(quantity_milli) OVER (references_by_kind ORDER BY id
                    ROWS UNBOUNDED PRECEDING) AS returned_so_far
            FROM inventory_movements
            WHERE ? IS NULL OR shop_id = ?
            WINDOW references_by_kind AS (PARTITION BY shop_id, item_id, kind, reverses_id)
        )
        SELECT m.shop_id, m.item_id, m.id, m.kind,
            (i.id IS NULL OR u.id IS NULL) AS orphan,
            (m.kind NOT IN ('receive', 'adjust', 'reversal', 'consume', 'return')
                OR typeof(m.quantity_milli) <> 'integer' OR m.quantity_milli = 0
                OR m.quantity_milli NOT BETWEEN -1000000000 AND 1000000000
                OR (m.kind = 'receive' AND m.quantity_milli < 0)
                OR (m.kind = 'consume' AND m.quantity_milli > 0)
                OR (m.kind NOT IN ('reversal', 'return') AND m.reverses_id IS NOT NULL)) AS bad_movement,
            (m.on_hand_after_milli IS NOT m.running_balance
                OR m.running_balance NOT BETWEEN 0 AND 1000000000000) AS bad_balance,
            (original.id IS NULL OR original.id >= m.id OR original.item_id IS NOT m.item_id) AS bad_original,
            (original.kind NOT IN ('receive', 'adjust') OR m.references_count <> 1
                OR m.quantity_milli IS NOT -original.quantity_milli) AS bad_reversal,
            (original.kind IS NOT 'consume' OR original.quantity_milli >= 0
                OR m.quantity_milli <= 0) AS bad_return,
            (m.returned_so_far > -original.quantity_milli) AS over_return
        FROM ledger AS m
        LEFT JOIN inventory_items AS i ON i.shop_id = m.shop_id AND i.id = m.item_id
        LEFT JOIN users AS u ON u.shop_id = m.shop_id AND u.id = m.actor_user_id
        LEFT JOIN inventory_movements AS original
            ON original.shop_id = m.shop_id AND original.id = m.reverses_id
        ORDER BY m.shop_id, m.id
    """, scope)
    issues = []
    for row in rows:
        kind = row["kind"]
        for code, failed in (
            ("orphan_movement", row["orphan"]),
            ("invalid_movement", row["bad_movement"]),
            ("movement_balance", row["bad_balance"]),
            ("invalid_reversal_link", kind == "reversal" and (row["bad_original"] or row["bad_reversal"])),
            ("invalid_return_link", kind == "return" and (row["bad_original"] or row["bad_return"])),
            ("over_return", kind == "return" and not row["bad_original"] and not row["bad_return"]
             and row["over_return"]),
        ):
            if failed:
                issues.append(StockIssue(code, row["shop_id"], row["item_id"], row["id"]))
    return issues


def consumption_link_issues(db, scope) -> list[StockIssue]:
    rows = db.execute("""
        WITH links AS (
            SELECT *, COUNT(*) OVER (PARTITION BY shop_id, charge_id) AS charge_links
            FROM repair_part_consumptions WHERE ? IS NULL OR shop_id = ?
        )
        SELECT 'invalid_consumption_link' AS code, p.shop_id AS shop_id, m.item_id AS item_id,
               p.movement_id AS movement_id, p.charge_id AS charge_id
        FROM links AS p
        LEFT JOIN inventory_movements AS m ON m.shop_id = p.shop_id AND m.id = p.movement_id
        LEFT JOIN repair_charges AS c ON c.shop_id = p.shop_id AND c.id = p.charge_id
        LEFT JOIN repairs AS r ON r.shop_id = c.shop_id AND r.id = c.repair_id
        WHERE m.kind IS NOT 'consume' OR m.quantity_milli >= 0 OR c.kind IS NOT 'part' OR r.id IS NULL
        UNION
        SELECT 'duplicate_charge_link', p.shop_id, m.item_id, p.movement_id, p.charge_id
        FROM links AS p
        LEFT JOIN inventory_movements AS m ON m.shop_id = p.shop_id AND m.id = p.movement_id
        WHERE p.charge_links > 1
        UNION ALL
        SELECT 'consumption_link_count', m.shop_id, m.item_id, m.id, NULL
        FROM inventory_movements AS m
        LEFT JOIN links AS p ON p.shop_id = m.shop_id AND p.movement_id = m.id
        WHERE m.kind = 'consume' AND (? IS NULL OR m.shop_id = ?)
        GROUP BY m.shop_id, m.id HAVING COUNT(p.movement_id) <> 1
        ORDER BY shop_id, movement_id, charge_id, code
    """, scope + scope)
    return [StockIssue(**row) for row in rows]


def includes_snapshot(value, expected: dict) -> bool:
    return isinstance(value, dict) and all(
        key in value and type(value[key]) is type(wanted) and value[key] == wanted
        for key, wanted in expected.items()
    )


def imported_stock_audits(db, scope):
    audits = {}
    for row in db.execute("""
        SELECT a.shop_id, a.actor_user_id, a.operation, a.entity_type, a.entity_id, a.changes,
               p.last_movement_id
        FROM audit_events AS a JOIN portable_imports AS p ON p.shop_id = a.shop_id
        WHERE p.last_movement_id IS NOT NULL AND (? IS NULL OR a.shop_id = ?)
            AND a.operation IN ('inventory.consume', 'inventory.return', 'inventory.intake_mistake')
    """, scope):
        try:
            changes = json.loads(row["changes"])
        except (ValueError, TypeError, RecursionError):
            continue
        movement_id = changes.get("movement_id") if isinstance(changes, dict) else None
        if type(movement_id) is int and 1 <= movement_id <= row["last_movement_id"]:
            audits.setdefault((row["shop_id"], movement_id), []).append((row, changes))
    return audits


def matches_imported_snapshot(row, audits) -> bool:
    if len(audits) != 1 or row["receipt_count"] is not None or row["item_unit"] != row["unit"]:
        return False
    audit, changes = audits[0]
    if (audit["entity_type"] != "charge" or audit["entity_id"] != row["charge_id"]
            or audit["actor_user_id"] != row["actor_user_id"]):
        return False
    expected = {
        "repair_id": row["repair_id"], "item_id": row["item_id"], "movement_id": row["id"],
    }
    if row["kind"] == "consume":
        operations = {"inventory.consume"}
        expected["consumed_quantity_milli"] = -row["quantity_milli"]
    else:
        operations = {"inventory.return", "inventory.intake_mistake"}
        expected |= {"reverses_id": row["reverses_id"], "quantity_milli": row["quantity_milli"]}
    return (
        audit["operation"] in operations and includes_snapshot(changes, expected)
        and includes_snapshot(changes.get("on_hand_milli"), {
            "before": row["on_hand_after_milli"] - row["quantity_milli"],
            "after": row["on_hand_after_milli"],
        })
    )


def repair_snapshot_issues(db, scope, *, portable: bool = False) -> list[StockIssue]:
    # Consumption receipts retain the original charge and catalog values.
    # Physical return receipts anchor the original consumption even if two uses share an item.
    audits = imported_stock_audits(db, scope) if portable else {}
    imported = "m.id <= p_import.last_movement_id" if portable else "NULL"
    import_join = "LEFT JOIN portable_imports AS p_import ON p_import.shop_id = m.shop_id" if portable else ""
    rows = db.execute(f"""
        WITH receipts AS (
            SELECT shop_id, movement_id, COUNT(*) AS receipt_count, MIN(result) AS result,
                   MIN(operation) AS operation, MIN(actor_id) AS actor_id
            FROM stock_requests WHERE ? IS NULL OR shop_id = ?
            GROUP BY shop_id, movement_id
        )
        SELECT m.shop_id, m.item_id, m.id, m.kind, m.quantity_milli, m.on_hand_after_milli, m.reverses_id,
            m.actor_user_id, used.id AS used_id, used.quantity_milli AS used_quantity,
            c.id AS charge_id, c.repair_id, c.kind AS charge_kind, c.description, c.unit, c.unit_cost_cents,
            i.unit AS item_unit, sr.receipt_count, sr.result, sr.operation, sr.actor_id,
            {imported} AS imported
        FROM inventory_movements AS m
        JOIN inventory_movements AS used ON used.shop_id = m.shop_id
            AND used.id = CASE m.kind WHEN 'consume' THEN m.id ELSE m.reverses_id END
            AND used.kind = 'consume' AND used.item_id = m.item_id
        JOIN repair_part_consumptions AS p ON p.shop_id = used.shop_id AND p.movement_id = used.id
        JOIN repair_charges AS c ON c.shop_id = p.shop_id AND c.id = p.charge_id
        LEFT JOIN inventory_items AS i ON i.shop_id = used.shop_id AND i.id = used.item_id
        LEFT JOIN receipts AS sr ON sr.shop_id = m.shop_id AND sr.movement_id = m.id
        {import_join}
        WHERE m.kind IN ('consume', 'return') AND (? IS NULL OR m.shop_id = ?)
        ORDER BY m.shop_id, m.id, c.id
    """, scope + scope)
    issues = set()
    for row in rows:
        if row["imported"]:
            valid = matches_imported_snapshot(row, audits.get((row["shop_id"], row["id"]), ()))
        else:
            valid = matches_repair_snapshot(row)
        if not valid:
            code = "consumption_snapshot" if row["kind"] == "consume" else "return_snapshot"
            issues.add(StockIssue(code, row["shop_id"], row["item_id"], row["id"], row["charge_id"]))
    return sorted(issues, key=lambda issue: (issue.shop_id, issue.movement_id, issue.charge_id))


def matches_repair_snapshot(row) -> bool:
    if row["receipt_count"] != 1 or row["actor_id"] != row["actor_user_id"] or row["item_unit"] != row["unit"]:
        return False
    try:
        result = json.loads(row["result"])
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(result, dict):
        return False
    expected_charge = {
        "id": row["charge_id"], "repair_id": row["repair_id"], "kind": row["charge_kind"],
        "description": row["description"], "unit": row["unit"], "unit_cost_cents": row["unit_cost_cents"],
        "inventory_item_id": row["item_id"], "movement_id": row["used_id"],
        "consumed_quantity_milli": -row["used_quantity"],
    }
    if row["kind"] == "consume":
        return row["operation"] == "inventory.consume" and includes_snapshot(result, expected_charge)
    change = result.get("stock")
    return (
        row["operation"] in {"inventory.return", "inventory.intake_mistake"}
        and includes_snapshot(result.get("charge"), expected_charge)
        and isinstance(change, dict)
        and includes_snapshot(change.get("movement"), {
            "id": row["id"], "item_id": row["item_id"], "kind": "return", "reverses_id": row["reverses_id"],
            "quantity_milli": row["quantity_milli"], "on_hand_after_milli": row["on_hand_after_milli"],
        })
    )
