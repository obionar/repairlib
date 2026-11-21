"""Data operations for the standalone workspace lifecycle."""

from contextlib import closing
from pathlib import Path
import sqlite3

from repairlib.storage.initialization import DEMO_CUSTOMERS, DEMO_SEED_MARKER, seed_demo_customers
from repairlib.storage.sessions import connect


class WorkspaceDataError(Exception):
    """Workspace data could not be read or safely reset."""


def local_shop(db: sqlite3.Connection) -> int:
    row = db.execute("""
        SELECT w.shop_id FROM local_workspace AS w
        JOIN shops AS s ON s.id = w.shop_id
        JOIN shop_settings AS settings ON settings.shop_id = w.shop_id
        WHERE w.id = 1
    """).fetchone()
    if row is None:
        raise WorkspaceDataError("The local shop or its settings are missing.")
    return row[0]


def demo_seeded(db: sqlite3.Connection, shop_id: int) -> bool:
    return db.execute("""
        SELECT 1 FROM audit_events
        WHERE shop_id = ? AND actor_kind = 'maintenance' AND actor_user_id IS NULL
            AND operation = 'demo.seed' AND source = 'cli' AND changes = ?
        LIMIT 1
    """, (shop_id, DEMO_SEED_MARKER)).fetchone() is not None


def workspace_counts(path: Path, *, stopped: bool) -> dict:
    # An immutable reader avoids creating SQLite sidecars on a clean stopped DB.
    # Use the WAL-aware reader whenever a writer may be active or a WAL exists.
    mode = "ro"
    if stopped and not path.with_name(path.name + "-wal").exists():
        mode = "ro&immutable=1"
    try:
        with closing(connect(path, mode)) as db:
            db.execute("BEGIN")
            shop_id = local_shop(db)
            customers = db.execute("SELECT count(*) FROM customers WHERE shop_id = ?", (shop_id,))
            repairs = db.execute("SELECT count(*) FROM repairs WHERE shop_id = ?", (shop_id,))
            return {
                "customers": customers.fetchone()[0],
                "repairs": repairs.fetchone()[0],
                "demo_seeded": demo_seeded(db, shop_id),
            }
    except sqlite3.Error as error:
        raise WorkspaceDataError("Could not read workspace counts.") from error


# The demo seeds only customers. Any other business row or stored business
# request prevents reset, even if the visible customer count is unchanged.
DEMO_EMPTY_TABLES = (
    "customer_requests", "csv_requests", "repairs", "work_entries", "work_entry_revisions", "repair_requests",
    "attachments", "attachment_links", "attachment_requests", "repair_charges",
    "repair_discounts", "charge_requests",
)


def reset_demo_records(path: Path) -> None:
    try:
        with closing(connect(path, "rw")) as db, db:
            db.execute("BEGIN IMMEDIATE")
            shop_id = local_shop(db)
            if db.execute("SELECT count(*) FROM shops").fetchone() != (1,):
                raise WorkspaceDataError("Demo reset requires exactly one shop.")
            if not demo_seeded(db, shop_id):
                raise WorkspaceDataError("Workspace has no matching synthetic demo seed marker.")
            expected = [(number, shop_id, *record, 1, None)
                        for number, record in enumerate(DEMO_CUSTOMERS, start=1)]
            rows = db.execute("""
                SELECT id, shop_id, name, phone, email, address, notes, revision, archived_at
                FROM customers ORDER BY id LIMIT ?
            """, (len(expected) + 1,)).fetchall()
            if rows != expected or any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                                       for table in DEMO_EMPTY_TABLES):
                raise WorkspaceDataError("Synthetic demo business content has changed.")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise WorkspaceDataError("Workspace has invalid relationships.")

            db.execute("DELETE FROM customers WHERE shop_id = ?", (shop_id,))
            seed_demo_customers(db, shop_id)
            db.execute("""
                INSERT INTO audit_events(shop_id, actor_kind, operation, source, changes)
                VALUES (?, 'maintenance', 'demo.reset', 'cli', ?)
            """, (shop_id, DEMO_SEED_MARKER))
    except sqlite3.Error as error:
        raise WorkspaceDataError("Could not reset the synthetic demo database.") from error
