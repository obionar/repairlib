"""Schema-21 shop projections and isolated relational validation."""

from contextlib import closing
import json
import sqlite3

from repairlib.storage.initialization import apply_migration
from repairlib.storage.stock import mismatches
from repairlib.storage.stock_reconciliation import consumption_link_issues, movement_issues

SCHEMA = 21
MAX_RECORDS = 50_000
MAX_ID = (1 << 63) - 1

# These columns define format 1. Adding a database column never adds it to the export.
COLUMNS = {
    "shop_settings": "shop_id timezone revision max_upload_mib inline_preview_mib",
    "users": "id shop_id username role active revision",
    "customers": "id shop_id name phone email address notes revision created_at archived_at",
    "repairs": (
        "id shop_id customer_id technician_id device_type brand model serial display_device "
        "reported_issue notes stage revision created_at picked_up_at archived_at"
    ),
    "work_entries": "id shop_id repair_id author_id author_username kind body created_at revision deleted_at",
    "work_entry_revisions": (
        "id shop_id entry_id revision kind body editor_id editor_username reason deleted created_at"
    ),
    "attachments": (
        "id shop_id repair_id uploader_id uploader_username filename media_type size_bytes sha256 "
        "revision detached_at created_at purged_at purged_by_username caption"
    ),
    "attachment_links": "shop_id repair_id attachment_id entry_id",
    "inventory_items": (
        "id shop_id sku description category manufacturer part_number package specifications bin unit "
        "low_stock_milli purchase_cost_cents selling_price_cents created_at updated_at archived_at "
        "revision on_hand_milli"
    ),
    "inventory_movements": (
        "id shop_id item_id kind quantity_milli on_hand_after_milli reason reverses_id actor_user_id created_at"
    ),
    "repair_charges": (
        "id shop_id repair_id kind description quantity_milli unit unit_price_cents unit_cost_cents notes "
        "revision deleted_at created_by created_by_username created_at"
    ),
    "repair_discounts": "shop_id repair_id kind value revision",
    "repair_part_consumptions": "shop_id charge_id movement_id",
    "estimates": (
        "id shop_id repair_id version status note parts_subtotal_cents labor_subtotal_cents subtotal_cents "
        "discount_kind discount_value discount_cents total_cents created_by created_by_username created_at "
        "accepted_by accepted_by_username accepted_at"
    ),
    "estimate_lines": (
        "id shop_id estimate_id position kind description quantity_milli unit unit_price_cents line_total_cents"
    ),
    "audit_events": "id shop_id actor_kind actor_user_id operation occurred_at actor_username source "
                    "entity_type entity_id changes",
}
AUDIT_TARGETS = {
    "attachment": ("attachments", "id"),
    "charge": ("repair_charges", "id"),
    "customer": ("customers", "id"),
    "estimate": ("estimates", "id"),
    "inventory_item": ("inventory_items", "id"),
    "repair": ("repairs", "id"),
    "repair_discount": ("repairs", "id"),  # audit identifies the repair
    "shop_settings": ("shop_settings", "shop_id"),
    "user": ("users", "id"),
    "work_entry": ("work_entries", "id"),
}
MAINTENANCE_HISTORY = ("setup", "demo.seed", "demo.reset")
# Estimates were retired at schema 36. Format 1 keeps their keys so older builds still read our archives;
# exports carry no estimate rows and imports validate, then drop, any estimate rows and their history.
RETIRED = ("estimates", "estimate_lines")
LIVE_AUDIT_TARGETS = {entity: target for entity, target in AUDIT_TARGETS.items() if target[0] not in RETIRED}


class RecordError(ValueError):
    """Records fail the portable schema or contain invalid references."""


def source_name(db, shop_id: int) -> str:
    row = db.execute("SELECT name FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if row is None:
        raise RecordError("The source shop is missing.")
    return row[0]


def rows(db, shop_id: int, table: str):
    if table in RETIRED:
        return
    projection = ", ".join(COLUMNS[table].split())
    if table == "audit_events":
        # Schema 21 knows only person and maintenance actors, so an agent's event travels as the
        # person's event with the source "agent" rather than as a value older validators refuse.
        projection = projection.replace("actor_kind,", "CASE WHEN actor_kind = 'agent' THEN 'user' "
                                        "ELSE actor_kind END AS actor_kind,").replace(
            "source,", "CASE WHEN actor_kind = 'agent' THEN 'agent' ELSE source END AS source,")
        types = ", ".join("?" for _ in LIVE_AUDIT_TARGETS)
        yield from db.execute(f"""
            SELECT {projection} FROM audit_events WHERE shop_id = ? AND (
                (actor_kind IN ('user', 'agent') AND entity_type IN ({types}))
                OR (actor_kind = 'maintenance' AND entity_type IS NULL AND operation IN (?, ?, ?))
            ) ORDER BY id
            """, (shop_id, *LIVE_AUDIT_TARGETS, *MAINTENANCE_HISTORY))
    else:
        # All these schema-21 tables have rowids, including the composite link tables.
        yield from db.execute(f"SELECT {projection} FROM {table} WHERE shop_id = ? ORDER BY rowid",
                              (shop_id,))


def retained_storage(db, shop_id: int):
    return db.execute("""
        SELECT id, storage_folder || storage_name FROM attachments
        WHERE shop_id = ? AND purged_at IS NULL ORDER BY id
    """, (shop_id,))


def add_rows(db, table: str, rows: list, shop_id: int, *, storage_names=None) -> None:
    columns = COLUMNS[table].split()
    types = {column: {"TEXT": str, "INTEGER": int}[kind]
             for _, column, kind, *_ in db.execute(f"PRAGMA table_info({table})")
             if column in columns}
    present = {column for _, column, *_ in db.execute(f"PRAGMA table_info({table})")}
    # Validation runs at the portable schema, which predates 0039 image folders.
    extra = [column for column in {"users": ["password_hash"],
                                   "attachments": ["storage_folder", "storage_name"]}.get(table, [])
             if column in present]
    fields = columns + extra
    insert = f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})"

    def values():
        for row in rows:
            if not isinstance(row, dict) or row.keys() != types.keys():
                raise RecordError("A record contains unexpected or missing columns.")
            for column, expected in types.items():
                value = row[column]
                if value is None and column != "id":
                    continue  # SQL enforces remaining nullability.
                if (type(value) is not expected
                        or (expected is int and not -MAX_ID - 1 <= value <= MAX_ID)
                        or (expected is int and (column == "id" or column.endswith("_id")) and value <= 0)):
                    raise RecordError("A record contains an invalid field type or ID.")
            if row["shop_id"] != shop_id:
                raise RecordError("A record belongs to a different source shop.")
            data = [row[column] for column in columns]
            # Portable rows contain neither usable credentials nor internal paths.
            if table == "users":
                data.append("")
            elif table == "attachments":
                stored = storage_names[row["id"]] if storage_names is not None else f"{row['id']:032x}"
                folder, slash, name = stored.rpartition("/")
                data.extend((folder + slash, name) if "storage_folder" in extra else (stored,))
            yield data

    db.executemany(insert, values())


def check_audit_targets(db) -> None:
    for entity, (table, column) in AUDIT_TARGETS.items():
        if db.execute(f"""
            SELECT 1 FROM audit_events AS a
            WHERE a.entity_type = ? AND (a.actor_kind != 'user' OR NOT EXISTS (
                SELECT 1 FROM {table} AS target WHERE target.{column} = a.entity_id
            )) LIMIT 1
        """, (entity,)).fetchone():
            raise RecordError("An audit event references an invalid business record.")


def validate_records(records: dict, shop_id: int, name: str) -> None:
    # Reuse the trusted migrations for CHECK, UNIQUE and composite FK constraints.
    # Archive data is inserted as parameters into a database that exists only in RAM.
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            apply_migration(db, 1)
            db.execute("INSERT INTO shops(id, name) VALUES (?, ?)", (shop_id, name))
            for version in range(2, SCHEMA + 1):
                apply_migration(db, version)
            db.execute("PRAGMA defer_foreign_keys = ON")
            for table, rows in records.items():
                add_rows(db, table, rows, shop_id)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RecordError("A record references a missing or unrelated record.")
            check_audit_targets(db)
            scope = (shop_id, shop_id)
            if (mismatches(db, shop_id)[1] or movement_issues(db, scope)
                    or consumption_link_issues(db, scope)):
                raise RecordError("The stock ledger or its repair links are inconsistent.")
    except (sqlite3.Error, UnicodeError) as error:
        raise RecordError("Records violate the portable shop schema.") from error


REFERENCES = {
    "repairs": {"customer_id": "customers", "technician_id": "users"},
    "work_entries": {"repair_id": "repairs", "author_id": "users"},
    "work_entry_revisions": {"entry_id": "work_entries", "editor_id": "users"},
    "attachments": {"repair_id": "repairs", "uploader_id": "users"},
    "attachment_links": {"repair_id": "repairs", "attachment_id": "attachments", "entry_id": "work_entries"},
    "inventory_movements": {"item_id": "inventory_items", "reverses_id": "inventory_movements",
                            "actor_user_id": "users"},
    "repair_charges": {"repair_id": "repairs", "created_by": "users"},
    "repair_discounts": {"repair_id": "repairs"},
    "repair_part_consumptions": {"charge_id": "repair_charges", "movement_id": "inventory_movements"},
    "estimates": {"repair_id": "repairs", "created_by": "users", "accepted_by": "users"},
    "estimate_lines": {"estimate_id": "estimates"},
    "audit_events": {"actor_user_id": "users"},
}
AUDIT_REFERENCES = {
    "customer_id": "customers", "technician_id": "users", "repair_id": "repairs",
    "entry_id": "work_entries", "item_id": "inventory_items", "movement_id": "inventory_movements",
    "reverses_id": "inventory_movements",
}
BUSINESS_TABLES = tuple(table for table in COLUMNS
                        if table not in {"shop_settings", "users", "audit_events", *RETIRED})


class DestinationUnavailable(ValueError):
    """The destination cannot allocate the records at its current state."""


def import_receipt(db, shop_id: int):
    return db.execute("SELECT * FROM portable_imports WHERE shop_id = ?", (shop_id,)).fetchone()


def is_empty(db, shop_id: int) -> bool:
    return all(db.execute(f"SELECT 1 FROM {table} WHERE shop_id = ? LIMIT 1", (shop_id,)).fetchone()
               is None for table in BUSINESS_TABLES)


def settings_revision(db, shop_id: int) -> int:
    return db.execute("SELECT revision FROM shop_settings WHERE shop_id = ?", (shop_id,)).fetchone()[0]


def allocate_ids(db, records: dict, shop_id: int):
    maps = {}
    for table, projection in COLUMNS.items():
        if not projection.startswith("id ") or table in RETIRED:
            continue
        base = db.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0]
        source_ids = sorted(row["id"] for row in records[table])
        if source_ids and max(base, 0) + len(source_ids) >= MAX_ID:
            raise DestinationUnavailable("The destination cannot allocate IDs for this archive.")
        maps[table] = dict(zip(source_ids, range(max(base, 0) + 1, max(base, 0) + 1 + len(source_ids))))
    maps["shop_settings"] = {records["shop_settings"][0]["shop_id"]: shop_id}
    return maps


def mapped_id(maps, table, old):
    if old is None:
        return None
    if type(old) is not int or old not in maps[table]:
        raise RecordError("A historical reference cannot be mapped.")
    return maps[table][old]


def map_history(row, maps):
    if row["entity_type"] is not None:
        table, _ = AUDIT_TARGETS[row["entity_type"]]
        row["entity_id"] = mapped_id(maps, table, row["entity_id"])
    if row["changes"] is None:
        return
    changes = json.loads(row["changes"])
    for field, table in AUDIT_REFERENCES.items():
        if field not in changes:
            continue
        value = changes[field]
        if isinstance(value, dict) and value.keys() == {"before", "after"}:
            changes[field] = {side: mapped_id(maps, table, ref) for side, ref in value.items()}
        else:
            changes[field] = mapped_id(maps, table, value)
    row["changes"] = json.dumps(changes)


def map_records(db, records: dict, shop_id: int):
    maps = allocate_ids(db, records, shop_id)
    occupied = {row[0] for row in db.execute("SELECT username FROM users WHERE shop_id = ?", (shop_id,))}
    reserved = occupied | {row["username"] for row in records["users"]}
    mapped = {}
    for table in COLUMNS:
        if table in RETIRED:
            continue
        mapped[table] = []
        for original in sorted(records[table], key=lambda row: row.get("id", 0)):
            if table == "audit_events" and original["entity_type"] not in (None, *LIVE_AUDIT_TARGETS):
                continue
            row = original | {"shop_id": shop_id}
            if "id" in row:
                row["id"] = maps[table][row["id"]]
            for field, target in REFERENCES.get(table, {}).items():
                row[field] = mapped_id(maps, target, row[field])
            if table == "users":
                row["active"] = 0
                if row["username"] in occupied:
                    candidate = f"imported-{row['id']}"
                    while candidate in reserved:
                        candidate += "_"
                        if len(candidate) > 80:
                            raise DestinationUnavailable("The destination cannot allocate an imported login.")
                    reserved.add(candidate)
                    row["username"] = candidate
            elif table == "audit_events":
                map_history(row, maps)
            mapped[table].append(row)
    return mapped, maps


def insert_import(db, mapped, shop_id: int, storage_names, revision: int, *,
                  actor_id: int, key: str, fingerprint: str, result: dict) -> None:
    settings = mapped["shop_settings"][0]
    updated = db.execute("""
        UPDATE shop_settings SET timezone = ?, max_upload_mib = ?, inline_preview_mib = ?,
            revision = revision + 1 WHERE shop_id = ? AND revision = ?
    """, (settings["timezone"], settings["max_upload_mib"], settings["inline_preview_mib"],
          shop_id, revision))
    if not updated.rowcount:
        raise DestinationUnavailable("The destination settings changed. Review the destination again.")
    try:
        for table, records in mapped.items():
            if table != "shop_settings":
                add_rows(db, table, records, shop_id, storage_names=storage_names)
        movements = mapped["inventory_movements"]
        db.execute("""
            INSERT INTO portable_imports (shop_id, actor_id, request_key, fingerprint, result, last_movement_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (shop_id, actor_id, key, fingerprint, json.dumps(result),
              movements[-1]["id"] if movements else None))
    except sqlite3.IntegrityError as error:
        raise RecordError("Mapped records violate destination schema constraints.") from error
