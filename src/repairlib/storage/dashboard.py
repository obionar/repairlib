"""Bounded, shop-scoped repair queue queries."""

from repairlib.contracts import QueueRepair


def repair_queue(db, shop_id: int, stages: tuple[str, ...], *, awaiting_pickup: bool,
                 limit: int) -> tuple[list[QueueRepair], int]:
    stage_placeholders = ", ".join("?" for _ in stages)
    filters = f"r.shop_id = ? AND r.archived_at IS NULL AND r.stage IN ({stage_placeholders})"
    parameters = (shop_id, *stages)
    joins = ""
    timestamp = "r.created_at"
    stage_order = " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(stages))
    order = f"CASE r.stage {stage_order} END, r.created_at, r.id"
    order_parameters = stages
    if awaiting_pickup:
        filters += " AND r.picked_up_at IS NULL"
        # These events correspond to RepairFlow's mutations of the repair's updated date.
        joins = """LEFT JOIN (
            SELECT entity_id, MAX(id) AS audit_id
            FROM audit_events
            WHERE shop_id = ? AND entity_type = 'repair'
                AND operation IN ('repair.update', 'repair.stage', 'repair.pickup', 'repair.archive')
            GROUP BY entity_id
        ) AS latest ON latest.entity_id = r.id
        LEFT JOIN audit_events AS change ON change.id = latest.audit_id AND change.shop_id = r.shop_id"""
        parameters = (shop_id, *parameters)
        timestamp = "COALESCE(change.occurred_at, r.created_at)"
        order = f"{timestamp}, r.id"
        order_parameters = ()

    rows = db.execute(
        f"""
        SELECT r.id, r.customer_id, c.name AS customer_name, r.device_type, r.brand, r.model,
               r.display_device, r.reported_issue, r.stage, r.picked_up_at, {timestamp} AS timestamp,
               u.username AS technician_username,
               COUNT(*) OVER () AS total
        FROM repairs AS r
        LEFT JOIN customers AS c ON c.shop_id = r.shop_id AND c.id = r.customer_id
        LEFT JOIN users AS u ON u.shop_id = r.shop_id AND u.id = r.technician_id
        {joins}
        WHERE {filters}
        ORDER BY {order}
        LIMIT ?
        """, (*parameters, *order_parameters, limit),
    ).fetchall()
    repairs = []
    total = 0
    for row in rows:
        values = dict(row)
        total = values.pop("total")
        repairs.append(QueueRepair(**values))
    return repairs, total
