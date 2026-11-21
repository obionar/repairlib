"""Audit-log reads that are not tied to one feature."""


def newest_occurred_at(db):
    """Timestamp of the newest audit event, or None on an empty log."""
    return db.execute("SELECT MAX(occurred_at) FROM audit_events").fetchone()[0]
