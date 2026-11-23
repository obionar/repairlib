"""Shop-scoped repairs, work entries and atomic mutation bookkeeping."""


from repairlib.contracts import (
    OPEN_REPAIR_STAGES, DeviceRepair, Repair, RepairActivityEvent, RepairInput, RepairListRow, WorkEntry,
    WorkEntryRevision,
)
from repairlib.storage import request_ledger

COLUMNS = ("id, customer_id, technician_id, device_type, brand, model, serial, display_device, "
           "reported_issue, notes, stage, revision, created_at, picked_up_at, archived_at, warranty_until, "
           "warranty_repair_id, payment_paid")
ENTRY_COLUMNS = ("id, repair_id, author_id, author_username, kind, body, created_at, revision, "
                 "deleted_at IS NOT NULL AS deleted")


def list_page(db, shop_id: int, before: int | None, limit: int, words: tuple[str, ...] = (),
              stage: str | None = None, *, picked_up: bool | None = None, archived: bool | None = None,
              paid: bool | None = None,
              technician: int | None = None, unassigned: bool = False, customer_id: int | None = None,
              queue_stages: tuple[str, ...] = (), phone_pattern: str | None = None) -> tuple[list[RepairListRow], int]:
    filters = ["r.shop_id = ?"]
    params = [shop_id]
    if words:  # every word must match the device, issue or customer name; any order, any field
        text = r"""rl_fold(r.device_type || ' ' || r.brand || ' ' || r.model || ' ' || r.serial || ' '
                   || r.display_device || ' ' || r.reported_issue || ' ' || COALESCE(c.name, ''))"""
        matches = " AND ".join(f"{text} LIKE ? ESCAPE '\\'" for _ in words)
        filters.append(f"(({matches}) OR c.phone_digits LIKE ?)")
        params.extend((*words, phone_pattern))
    if queue_stages:
        filters.append(f"r.archived_at IS NULL AND r.stage IN ({', '.join('?' for _ in queue_stages)})")
        params.extend(queue_stages)
    for column, value in (("stage", stage), ("customer_id", customer_id)):
        if value is not None:
            filters.append(f"r.{column} = ?")
            params.append(value)
    if technician is not None or unassigned:
        filters.append("r.technician_id IS ?")
        params.append(technician)
    for column, value in (("picked_up_at", picked_up), ("archived_at", archived)):
        if value is not None:
            filters.append(f"(r.{column} IS NOT NULL) = ?")
            params.append(value)
    if paid is not None:  # unpaid includes repairs with no payment recorded
        filters.append("COALESCE(r.payment_paid, 0) = ?")
        params.append(paid)

    joins = """FROM repairs AS r
        LEFT JOIN customers AS c ON c.shop_id = r.shop_id AND c.id = r.customer_id"""
    where = " AND ".join(filters)
    total = db.execute(f"SELECT COUNT(*) {joins} WHERE {where}", params).fetchone()[0]
    columns = ", ".join(f"r.{column}" for column in COLUMNS.split(", "))
    rows = db.execute(
        f"""SELECT {columns}, c.name AS customer_name, u.username AS technician_username
        {joins}
        LEFT JOIN users AS u ON u.shop_id = r.shop_id AND u.id = r.technician_id
        WHERE {where} AND (? IS NULL OR r.id < ?)
        ORDER BY r.id DESC LIMIT ?""",
        (*params, before, before, limit),
    )
    return [RepairListRow(**row) for row in rows], total


def for_serial(db, shop_id: int, serial: str) -> list[DeviceRepair]:
    rows = db.execute("""
        SELECT id, created_at, stage, reported_issue
        FROM repairs WHERE shop_id = ? AND serial = ?
        ORDER BY created_at, id
        """, (shop_id, serial))
    return [DeviceRepair(**row) for row in rows]


def warranty_returns(db, shop_id: int, repair_id: int) -> list[DeviceRepair]:
    rows = db.execute("""
        SELECT id, created_at, stage, reported_issue
        FROM repairs WHERE shop_id = ? AND warranty_repair_id = ?
        ORDER BY created_at, id
        """, (shop_id, repair_id))
    return [DeviceRepair(**row) for row in rows]


def find(db, shop_id: int, repair_id: int) -> Repair | None:
    row = db.execute(f"SELECT {COLUMNS} FROM repairs WHERE shop_id = ? AND id = ?",
                     (shop_id, repair_id)).fetchone()
    return Repair(**row) if row is not None else None


def for_customer(db, shop_id: int, customer_id: int) -> list[Repair]:
    rows = db.execute(f"SELECT {COLUMNS} FROM repairs WHERE shop_id = ? AND customer_id = ? ORDER BY id",
                      (shop_id, customer_id))
    return [Repair(**row) for row in rows]


def customer_is_selectable(db, shop_id: int, customer_id: int, linked_customer_id: int | None) -> bool:
    return db.execute(
        "SELECT 1 FROM customers WHERE shop_id = ? AND id = ? AND (archived_at IS NULL OR id = ?)",
        (shop_id, customer_id, linked_customer_id),
    ).fetchone() is not None


def customer_name(db, shop_id: int, customer_id: int, linked_id: int | None) -> str | None:
    row = db.execute("SELECT name FROM customers WHERE shop_id = ? AND id = ? AND (archived_at IS NULL OR id = ?)",
                     (shop_id, customer_id, linked_id)).fetchone()
    return row[0] if row else None


def customer_options(db, shop_id: int, linked_id: int | None, selected: int | None) -> list[tuple[int, str]]:
    return db.execute(
        """
        SELECT id, name FROM customers
        WHERE shop_id = ? AND (archived_at IS NULL OR id = ?)
        ORDER BY CASE WHEN id IN (?, ?) THEN 0 ELSE 1 END, name, id LIMIT 100
        """, (shop_id, linked_id, linked_id, selected),
    ).fetchall()


def list_filter_technicians(db, shop_id: int) -> list[tuple[int, str]]:
    return db.execute(
        """SELECT u.id, u.username FROM users AS u
        WHERE u.shop_id = ? AND (u.role = 'technician' OR EXISTS (
            SELECT 1 FROM repairs AS r WHERE r.shop_id = u.shop_id AND r.technician_id = u.id
        ))
        ORDER BY u.username, u.id""", (shop_id,),
    ).fetchall()


def relationship_names(db, shop_id: int, repair: Repair) -> dict[str, str]:
    customer = db.execute("SELECT name, phone, email FROM customers WHERE shop_id = ? AND id = ?",
                          (shop_id, repair.customer_id)).fetchone() if repair.customer_id else None
    technician = db.execute("SELECT username FROM users WHERE shop_id = ? AND id = ?",
                            (shop_id, repair.technician_id)).fetchone() if repair.technician_id else None
    return {"customer": customer[0] if customer else "",
            "customer_phone": customer[1] if customer else "",
            "customer_email": customer[2] if customer else "",
            "technician": technician[0] if technician else ""}


def count_other_open_repairs(db, shop_id: int, repair: Repair) -> int:
    if repair.customer_id is None:
        return 0
    stages = ", ".join("?" for _ in OPEN_REPAIR_STAGES)
    return db.execute(
        f"""SELECT COUNT(*) FROM repairs WHERE shop_id = ? AND customer_id = ?
             AND id <> ? AND archived_at IS NULL AND stage IN ({stages})""",
        (shop_id, repair.customer_id, repair.id, *OPEN_REPAIR_STAGES),
    ).fetchone()[0]


def list_activity(db, shop_id: int, repair_id: int, before: int | None,
                  limit: int) -> list[RepairActivityEvent]:
    # Static table names; resolve children by their stored shop and repair, not audit payloads.
    children = (("work_entry", "work_entries"), ("charge", "repair_charges"),
                ("attachment", "attachments"))
    cursor = "AND id < ?" if before is not None else ""
    scope = (shop_id, before) if before is not None else (shop_id,)
    select_ids = f"SELECT id FROM audit_events WHERE shop_id = ? {cursor}"
    branches = [f"{select_ids} AND entity_type IN ('repair', 'repair_discount') AND entity_id = ?"]
    params = [*scope, repair_id]
    for entity, table in children:
        branches.append(f"""{select_ids} AND entity_type = ?
            AND entity_id IN (SELECT id FROM {table} WHERE shop_id = ? AND repair_id = ?)""")
        params.extend((*scope, entity, shop_id, repair_id))
    activity_ids = " UNION ALL ".join(branches)
    # Find the page using only index keys, then fetch metadata for those event IDs.
    rows = db.execute(
        f"""SELECT id, operation, occurred_at, actor_username, source, entity_type, entity_id, actor_kind
            FROM audit_events WHERE id IN ({activity_ids} ORDER BY id DESC LIMIT ?)
            ORDER BY id DESC""",
        (*params, limit),
    )
    return [RepairActivityEvent(**row) for row in rows]


def customer_phone(db, shop_id: int, repair: Repair) -> str:
    if repair.customer_id is None:
        return ""
    row = db.execute("SELECT phone FROM customers WHERE shop_id = ? AND id = ?",
                     (shop_id, repair.customer_id)).fetchone()
    return row[0] if row else ""


def active_technician(db, shop_id: int, user_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM users WHERE shop_id = ? AND id = ? AND role = 'technician' AND active = 1",
        (shop_id, user_id),
    ).fetchone() is not None


def insert(db, shop_id: int, values: RepairInput) -> Repair:
    row = db.execute(
        f"""
        INSERT INTO repairs(shop_id, customer_id, technician_id, device_type, brand, model,
                            serial, display_device, reported_issue, notes, warranty_until,
                            warranty_repair_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {COLUMNS}
        """, (shop_id, values.customer_id, values.technician_id, values.device_type,
              values.brand, values.model, values.serial, values.display_device,
              values.reported_issue, values.notes, values.warranty_until,
              values.warranty_repair_id),
    ).fetchone()
    return Repair(**row)


def update(db, shop_id: int, repair_id: int, revision: int, values: RepairInput) -> Repair | None:
    row = db.execute(
        f"""
        UPDATE repairs SET customer_id = ?, technician_id = ?, device_type = ?, brand = ?,
            model = ?, serial = ?, display_device = ?, reported_issue = ?, notes = ?,
            warranty_until = ?, warranty_repair_id = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? RETURNING {COLUMNS}
        """, (values.customer_id, values.technician_id, values.device_type, values.brand,
              values.model, values.serial, values.display_device, values.reported_issue,
              values.notes, values.warranty_until, values.warranty_repair_id, shop_id, repair_id,
              revision),
    ).fetchone()
    return Repair(**row) if row is not None else None


def update_stage(db, shop_id: int, repair_id: int, revision: int, stage: str) -> Repair | None:
    row = db.execute(
        f"""UPDATE repairs SET stage = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? RETURNING {COLUMNS}""",
        (stage, shop_id, repair_id, revision),
    ).fetchone()
    return Repair(**row) if row is not None else None


def bump_revision(db, shop_id: int, repair_id: int) -> None:
    db.execute("UPDATE repairs SET revision = revision + 1 WHERE shop_id = ? AND id = ?", (shop_id, repair_id))


def payment_status(db, shop_id: int, repair_id: int) -> bool | None:
    value = db.execute("SELECT payment_paid FROM repairs WHERE shop_id=? AND id=?", (shop_id, repair_id)).fetchone()[0]
    return None if value is None else bool(value)


def update_payment(db, shop_id: int, repair_id: int, revision: int, paid: bool) -> Repair | None:
    row = db.execute(f"""UPDATE repairs SET payment_paid = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? RETURNING {COLUMNS}""",
        (int(paid), shop_id, repair_id, revision)).fetchone()
    return Repair(**row) if row is not None else None


def update_flag(db, shop_id: int, repair_id: int, revision: int, field: str,
                enabled: bool, timestamp: str | None) -> Repair | None:
    if field not in {"picked_up_at", "archived_at"}:
        raise ValueError("Invalid repair flag")
    row = db.execute(
        f"""UPDATE repairs SET {field} = ?, revision = revision + 1
        WHERE shop_id = ? AND id = ? AND revision = ? RETURNING {COLUMNS}""",
        (timestamp if enabled else None, shop_id, repair_id, revision),
    ).fetchone()
    return Repair(**row) if row is not None else None


def save_result(db, shop_id: int, actor_id: int, operation: str, key: str,
                fingerprint: str, repair_id: int, result) -> None:
    request_ledger.store(db, "repair", shop_id, actor_id, operation, key, fingerprint, result,
                         link=repair_id)



def entry_record(row) -> WorkEntry:
    values = dict(row)
    values["deleted"] = bool(values["deleted"])
    return WorkEntry(**values)


def save_entry_revision(db, principal, entry: WorkEntry, reason: str) -> None:
    db.execute(
        """
        INSERT INTO work_entry_revisions
            (shop_id, entry_id, revision, kind, body, editor_id, editor_username, reason, deleted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (principal.shop_id, entry.id, entry.revision, entry.kind, entry.body,
              principal.user_id, principal.username, reason, int(entry.deleted)),
    )


def insert_entry(db, principal, repair_id: int, kind: str, body: str) -> WorkEntry:
    row = db.execute(
        f"""
        INSERT INTO work_entries(shop_id, repair_id, author_id, author_username, kind, body)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING {ENTRY_COLUMNS}
        """, (principal.shop_id, repair_id, principal.user_id, principal.username, kind, body),
    ).fetchone()
    entry = entry_record(row)
    save_entry_revision(db, principal, entry, "")
    return entry


def find_entry(db, shop_id: int, repair_id: int, entry_id: int) -> WorkEntry | None:
    row = db.execute(
        f"SELECT {ENTRY_COLUMNS} FROM work_entries WHERE shop_id = ? AND repair_id = ? AND id = ?",
        (shop_id, repair_id, entry_id),
    ).fetchone()
    return entry_record(row) if row is not None else None


def update_entry(db, principal, current: WorkEntry, *, kind: str, body: str,
                 reason: str, deleted: bool, timestamp: str | None) -> WorkEntry | None:
    row = db.execute(
        f"""
        UPDATE work_entries SET kind = ?, body = ?, revision = revision + 1, deleted_at = ?
        WHERE shop_id = ? AND repair_id = ? AND id = ? AND revision = ?
        RETURNING {ENTRY_COLUMNS}
        """, (kind, body, timestamp if deleted else None, principal.shop_id,
              current.repair_id, current.id, current.revision),
    ).fetchone()
    if row is None:
        return None
    entry = entry_record(row)
    save_entry_revision(db, principal, entry, reason)
    return entry


def entry_history(db, shop_id: int, repair_id: int, entry_id: int) -> list[WorkEntryRevision]:
    rows = db.execute(
        """
        SELECT revision, kind, body, editor_id, editor_username, reason, deleted, created_at
        FROM work_entry_revisions
        WHERE shop_id = ? AND entry_id = ? AND EXISTS (
            SELECT 1 FROM work_entries WHERE shop_id = ? AND repair_id = ? AND id = ?
        ) ORDER BY revision DESC
        """, (shop_id, entry_id, shop_id, repair_id, entry_id),
    )
    return [WorkEntryRevision(**(dict(row) | {"deleted": bool(row["deleted"])})) for row in rows]


def list_entries(db, shop_id: int, repair_id: int, before: int | None, limit: int, *,
                 live_only: bool = False) -> list[WorkEntry]:
    live_filter = "AND deleted_at IS NULL" if live_only else ""
    rows = db.execute(
        f"""SELECT {ENTRY_COLUMNS} FROM work_entries
        WHERE shop_id = ? AND repair_id = ? AND (? IS NULL OR id < ?) {live_filter}
        ORDER BY id DESC LIMIT ?""", (shop_id, repair_id, before, before, limit),
    )
    return [entry_record(row) for row in rows]


FEE_COLUMNS = {"diagnosis": "diagnosis_charge_id", "labor": "labor_charge_id"}


def fee_charge_ids(db, shop_id: int, repair_id: int) -> dict[str, int | None]:
    row = db.execute("SELECT diagnosis_charge_id, labor_charge_id FROM repair_fees "
                     "WHERE shop_id = ? AND repair_id = ?", (shop_id, repair_id)).fetchone()
    return {} if row is None else {"diagnosis": row[0], "labor": row[1]}


def save_fee(db, shop_id: int, repair_id: int, charge_id: int | None, name: str) -> None:
    try:
        column = FEE_COLUMNS[name]
    except KeyError:
        raise ValueError(f"Unknown fee: {name}") from None
    db.execute(f"""INSERT INTO repair_fees(shop_id, repair_id, {column}) VALUES (?, ?, ?)
        ON CONFLICT (shop_id, repair_id) DO UPDATE SET {column} = excluded.{column}""",
               (shop_id, repair_id, charge_id))
