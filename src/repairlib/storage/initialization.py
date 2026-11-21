"""Transactional schema management and read-only compatibility checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

MIGRATIONS = (
    "0001_workspace.sql",
    "0002_sessions.sql",
    "0003_api_tokens.sql",
    "0004_customers.sql",
    "0005_user_administration.sql",
    "0006_shop_settings.sql",
    "0007_repairs.sql",
    "0008_repair_pickup_archive.sql",
    "0009_work_entry_history.sql",
    "0010_attachments.sql",
    "0011_repair_charges.sql",
    "0012_estimates.sql",
    "0013_attachment_retention.sql",
    "0014_customer_archive.sql",
    "0015_attachment_metadata.sql",
    "0016_printer.sql",
    "0017_inventory_catalog.sql",
    "0018_inventory_stock.sql",
    "0019_repair_part_consumption.sql",
    "0020_repair_part_returns.sql",
    "0021_inventory_restore.sql",
    "0022_portable_import.sql",
    "0023_csv_intake.sql",
    "0024_sessions_idle.sql",
    "0025_request_receipt_age.sql",
    "0026_shop_language.sql",
    "0027_activity_index.sql",
    "0028_shop_contact.sql",
    "0029_customer_phone_digits.sql",
    "0030_warranty_and_device_history.sql",
    "0031_agent_tokens.sql",
    "0032_token_presets.sql",
    "0033_warranty_return.sql",
    "0034_repair_payment_status.sql",
    "0035_diagnosis_default.sql",
    "0036_drop_estimates.sql",
    "0037_repair_fees.sql",
    "0038_diagnosis_fee.sql",
    "0039_image_folders.sql",
    "0040_device_photos.sql",
)
SCHEMA_VERSION = len(MIGRATIONS)
MIGRATION_DIR = Path(__file__).with_name("migrations")


class UpgradeError(Exception):
    """The explicit schema upgrade failed."""


def migration_content(version: int) -> bytes:
    return (MIGRATION_DIR / MIGRATIONS[version - 1]).read_bytes()


def apply_migration(db: sqlite3.Connection, version: int) -> None:
    migration = migration_content(version)
    statement = ""
    for line in migration.decode("utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration statement")
    db.execute("INSERT INTO schema_migrations VALUES (?, ?)",
               (version, hashlib.sha256(migration).hexdigest()))
    db.execute(f"PRAGMA user_version = {version}")


def seed_workspace(
    db: sqlite3.Connection, shop: str, timezone: str, username: str, password_hash: str
) -> None:
    (shop_id,) = db.execute("INSERT INTO shops(name) VALUES (?) RETURNING id", (shop,)).fetchone()
    db.execute("INSERT INTO shop_settings(shop_id, timezone) VALUES (?, ?)", (shop_id, timezone))
    db.execute(
        "INSERT INTO users(shop_id, username, role, password_hash) VALUES (?, ?, 'admin', ?)",
        (shop_id, username, password_hash),
    )
    db.execute(
        "INSERT INTO audit_events(shop_id, actor_kind, operation) VALUES (?, 'maintenance', 'setup')",
        (shop_id,),
    )


DEMO_CUSTOMERS = (
    ("Synthetic Customer One", "+357 555 0100", "one@example.test",
     "1 Synthetic Street", "Synthetic demo record; not a real customer."),
    ("Synthetic Customer Two", "+357 555 0101", "two@example.test",
     "2 Synthetic Street", "Synthetic demo record; not a real customer."),
    ("Πελάτης Δοκιμής", "+357 555 0102", "greek@example.test",
     "Δοκιμαστική Οδός 3", "Synthetic demo record with Greek layout text."),
)
DEMO_SEED_MARKER = json.dumps({"synthetic": True, "customers": len(DEMO_CUSTOMERS)})


def seed_demo_customers(db: sqlite3.Connection, shop_id: int) -> None:
    db.executemany(
        """
        INSERT INTO customers(shop_id, name, phone, email, address, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """, [(shop_id, *record) for record in DEMO_CUSTOMERS],
    )


def seed_demo(db: sqlite3.Connection) -> None:
    shop_id = db.execute("SELECT shop_id FROM local_workspace WHERE id = 1").fetchone()[0]
    seed_demo_customers(db, shop_id)
    db.execute(
        """
        INSERT INTO audit_events(shop_id, actor_kind, operation, source, changes)
        VALUES (?, 'maintenance', 'demo.seed', 'cli', ?)
        """, (shop_id, DEMO_SEED_MARKER),
    )


def enable_wal(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA journal_mode = WAL").fetchone() != ("wal",):
        raise ValueError("Could not enable the SQLite WAL journal.")


def initialize_database(
    path: Path, shop: str, timezone: str, username: str, password_hash: str,
    *, target: int = SCHEMA_VERSION, demo: bool = False, language: str = "",
) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            apply_migration(db, 1)
            seed_workspace(db, shop, timezone, username, password_hash)
            for version in range(2, target + 1):
                apply_migration(db, version)
            if language:
                db.execute("UPDATE shop_settings SET language = ?", (language,))
            if demo:
                if target < 4:
                    raise ValueError("Synthetic demo data requires the customer schema.")
                seed_demo(db)
        if target >= 2:
            enable_wal(db)


def supported_version(db: sqlite3.Connection) -> int | None:
    (version,) = db.execute("PRAGMA user_version").fetchone()
    if not 1 <= version <= SCHEMA_VERSION:
        return None
    expected = [(number, hashlib.sha256(migration_content(number)).hexdigest())
                for number in range(1, version + 1)]
    history = db.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version").fetchall()
    return version if history == expected else None


def upgrade_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            version = supported_version(db)
            if version is None:
                raise UpgradeError("Workspace schema is unsupported.")
            if version < SCHEMA_VERSION:
                if db.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                    raise UpgradeError("Could not prepare the workspace for migration.")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    for number in range(version + 1, SCHEMA_VERSION + 1):
                        apply_migration(db, number)
            enable_wal(db)
    except sqlite3.Error as error:
        raise UpgradeError("Workspace schema upgrade could not complete.") from error


def schema_details(path: Path) -> tuple[str, int | None]:
    if path.with_name(path.name + "-journal").exists():
        return "upgrade_required", None
    try:
        # Explicit migrations commit schema and history to the main file before
        # enabling WAL. Runtime writes can only change data, so inspecting this
        # metadata snapshot never needs to open or create a WAL/SHM file.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            (reported_version,) = db.execute("PRAGMA user_version").fetchone()
            version = supported_version(db)
        if version is None:
            return "unsupported_schema", reported_version
        if version < SCHEMA_VERSION:
            return "upgrade_required", version
        with path.open("rb") as db_file:
            wal_header = db_file.read(20)[18:20] == b"\x02\x02"
        return ("initialized" if wal_header else "upgrade_required"), version
    except (OSError, sqlite3.Error):
        return "invalid", None


def schema_status(path: Path) -> str:
    return schema_details(path)[0]
