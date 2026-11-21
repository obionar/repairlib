"""One-command dry run: every storage transaction shares a connection that is never committed.

`armed()` is the only way in. While it is active, `transaction()` in the storage modules hands each
caller a SAVEPOINT on one shared, outer `BEGIN IMMEDIATE` connection instead of its own committing
one. Domain code therefore runs exactly as on the real path (validation, revision checks, receipts,
audit rows, even the first-use local actor) and a refusal still undoes its own work, but the outer
transaction is always rolled back, so nothing can reach the file. The audit rows the operation wrote
are read before the rollback and become the report.
"""

from contextlib import contextmanager
import contextvars
import json
from pathlib import Path
import sqlite3

from repairlib.storage.sessions import connect

_session: contextvars.ContextVar["Session | None"] = contextvars.ContextVar("dry_run", default=None)


class Session:
    def __init__(self):
        self.path: Path | None = None
        self.db: sqlite3.Connection | None = None
        self.depth = 0

    def open(self, path: Path) -> sqlite3.Connection:
        if self.db is None:
            self.path = path
            self.db = connect(path, "rw")
            self.db.row_factory = sqlite3.Row
            self.db.execute("BEGIN IMMEDIATE")
            self.start = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events").fetchone()[0]
        elif path != self.path:
            raise RuntimeError("A dry run covers one workspace.")
        return self.db

    def events(self) -> list[dict]:
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT operation, entity_type, entity_id, changes FROM audit_events WHERE id > ? "
            "AND operation <> 'user.local_actor' ORDER BY id", (self.start,))
        return [{"operation": row["operation"], "entity_type": row["entity_type"],
                 "entity_id": row["entity_id"], "changes": json.loads(row["changes"] or "{}")}
                for row in rows]

    def close(self) -> None:
        if self.db is not None:
            self.db.rollback()
            self.db.close()
            self.db = None


def active() -> Session | None:
    return _session.get()


@contextmanager
def armed():
    """Arm the guard for one command; yields the session whose `events()` describe the effect."""
    session = Session()
    token = _session.set(session)
    try:
        yield session
    finally:
        _session.reset(token)
        session.close()


@contextmanager
def scope(session: Session, path: Path):
    """A savepoint on the shared connection: released on success, undone on any exception."""
    db = session.open(path)
    session.depth += 1
    name = f"dry_run_{session.depth}"
    db.execute(f"SAVEPOINT {name}")
    try:
        yield db
    except BaseException:
        db.execute(f"ROLLBACK TO {name}")
        db.execute(f"RELEASE {name}")
        raise
    else:
        db.execute(f"RELEASE {name}")
    finally:
        session.depth -= 1
