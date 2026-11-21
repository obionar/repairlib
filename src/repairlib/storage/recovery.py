"""SQLite snapshot creation and validation for standalone workspace recovery."""

import os
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from time import monotonic

from repairlib.contracts import StockReconciliation
from repairlib.storage import stock_reconciliation
from repairlib.storage.initialization import apply_migration, enable_wal, seed_workspace, supported_version
from repairlib.storage.sessions import connect

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
ONLINE_BUSY_SECONDS = 30.0


class SnapshotError(Exception):
    """A database snapshot could not be created or verified."""


def sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def stop_on_busy(status: int, _remaining: int, _total: int) -> None:
    if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        raise SnapshotError("SQLite is busy. Stop all workspace writers before taking a snapshot.")


@contextmanager
def private_database_input(path: Path, directory: Path, prefix: str):
    """Copy stopped inputs so SQLite rebuilds shared memory only in private staging."""
    with TemporaryDirectory(prefix=prefix, dir=directory) as temporary:
        copy = Path(temporary) / path.name
        copyfile(path, copy)
        wal = sidecar(path, "-wal")
        if wal.exists():
            copyfile(wal, sidecar(copy, "-wal"))
        yield copy


def copy_database(source: Path, destination: Path) -> None:
    """Copy into a private staged file while the caller holds the workspace lock."""
    # Without a WAL, SQLite can read the main file in place without writes.
    # With a WAL, the backup API reads the committed view from private inputs
    # so a read-only SQLite connection cannot create or modify the source SHM.
    has_wal = sidecar(source, "-wal").exists()
    inputs = (private_database_input(source, destination.parent, ".wal-input-") if has_wal
              else nullcontext(source))
    mode = "ro" if has_wal else "ro&immutable=1"
    try:
        with (inputs as sqlite_input, closing(connect(sqlite_input, mode)) as original,
              closing(connect(destination, "rw")) as copy):
            original.backup(copy, pages=256, progress=stop_on_busy)
            if copy.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                raise SnapshotError("Could not make the SQLite snapshot independent of sidecars.")
    except sqlite3.Error as error:
        raise SnapshotError("Could not copy the workspace SQLite database.") from error


def online_wait(deadline: float):
    """Abort a backup step that cannot get past a writer before the deadline."""
    def progress(status: int, _remaining: int, _total: int) -> None:
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and monotonic() >= deadline:
            raise SnapshotError("SQLite stayed busy during the online snapshot. Try again when writes pause.")
    return progress


def online_copy_database(source: Path, destination: Path) -> None:
    """Back up the committed view of a running workspace through read-only SQLite."""
    try:
        with (closing(connect(source, "ro")) as original,
              closing(connect(destination, "rw")) as copy):
            original.backup(copy, pages=256, sleep=0.05,
                            progress=online_wait(monotonic() + ONLINE_BUSY_SECONDS))
            if copy.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                raise SnapshotError("Could not make the SQLite snapshot independent of sidecars.")
    except sqlite3.Error as error:
        raise SnapshotError("Could not copy the running workspace SQLite database. "
                            "Use a stopped snapshot if the app is not running.") from error


def verify_database_snapshot(path: Path) -> int:
    """Check a self-contained staged or published file without changing it."""
    with path.open("rb") as handle:
        header = handle.read(20)
    if not header.startswith(b"SQLite format 3\0") or header[18:20] != b"\x01\x01":
        raise SnapshotError("Expected a standalone SQLite snapshot in rollback-journal mode.")
    try:
        # SQLite 3.54.1 skips some CHECK constraint violations on connections
        # opened with mode=ro. Use query_only on the private, sidecar-free file.
        with closing(connect(path, "rw")) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("PRAGMA trusted_schema = OFF")
            if db.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
                raise SnapshotError("SQLite snapshot failed the integrity check.")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SnapshotError("SQLite snapshot failed the foreign key check.")
            version = supported_version(db)
            if version is None:
                raise SnapshotError("SQLite snapshot has an unsupported schema or migration history.")
            if schema_objects(db) != expected_schema_objects(version):
                raise SnapshotError("SQLite snapshot schema differs from its migration history.")
            return version
    except sqlite3.Error as error:
        raise SnapshotError("Could not verify the SQLite snapshot or its schema.") from error


def prepare_restored_database(path: Path) -> None:
    """Enable runtime journaling for a verified current-schema database."""
    try:
        with closing(connect(path, "rw")) as db:
            enable_wal(db)
    except (sqlite3.Error, ValueError) as error:
        raise SnapshotError("Could not enable runtime journaling for the restored database.") from error


def schema_objects(db: sqlite3.Connection) -> list[tuple]:
    return db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
    ).fetchall()


def expected_schema_objects(version: int) -> list[tuple]:
    with closing(sqlite3.connect(":memory:")) as db:
        apply_migration(db, 1)
        seed_workspace(db, "Schema check", "UTC", "schema-check", "unused")
        for number in range(2, version + 1):
            apply_migration(db, number)
        return schema_objects(db)


def snapshot_attachments(path: Path, version: int, limit: int) -> list[tuple[int, str, int, str]]:
    """Read the retained file inventory of an already verified standalone snapshot."""
    try:
        with closing(connect(path, "ro&immutable=1")) as db:
            db.execute("PRAGMA trusted_schema = OFF")
            return [row[1:] for row in retained_attachments(db, version, limit)]
    except sqlite3.Error as error:
        raise SnapshotError("Could not verify SQLite attachment metadata.") from error


def retained_attachments(db: sqlite3.Connection, version: int, limit: int = -1) -> list[tuple]:
    if version < 10:
        return []
    retained = "WHERE purged_at IS NULL" if version >= 13 else ""
    return db.execute(
        f"SELECT id, shop_id, storage_name, size_bytes, sha256 FROM attachments {retained} "
        "ORDER BY shop_id, id LIMIT ?", (limit,),
    ).fetchall()


def diagnostic_check(db: sqlite3.Connection, pragma: str, expected: tuple | None) -> str:
    try:
        return "ok" if db.execute(pragma).fetchone() == expected else "failed"
    except sqlite3.Error:
        return "unavailable"


def database_diagnostics(path: Path, version: int | None
                         ) -> tuple[str, str, list[tuple] | None, StockReconciliation | None]:
    """Inspect stopped, validated files without letting SQLite touch the source."""
    temporary_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
    if temporary_root.is_relative_to(path.parent.parent.resolve()):
        raise OSError("Doctor requires a temporary directory outside the workspace.")
    with private_database_input(path, temporary_root, "repairlib-doctor-") as copy:
        try:
            with closing(connect(copy, "rw")) as db:
                # A read-only SQLite connection can skip some CHECK constraints.
                db.execute("PRAGMA query_only = ON")
                db.execute("PRAGMA trusted_schema = OFF")
                integrity = diagnostic_check(db, "PRAGMA integrity_check(1)", ("ok",))
                foreign_keys = diagnostic_check(db, "PRAGMA foreign_key_check", None)
                try:
                    rows = retained_attachments(db, version) if version is not None else None
                except sqlite3.Error:
                    rows = None
                try:
                    db.row_factory = sqlite3.Row
                    stock = stock_reconciliation.reconcile(db, None, version) if version is not None else None
                except sqlite3.Error:
                    stock = None
                return integrity, foreign_keys, rows, stock
        except sqlite3.Error:
            return "unavailable", "unavailable", None, None
