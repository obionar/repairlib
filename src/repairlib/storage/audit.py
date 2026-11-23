"""The one audit-row writer for signed-in actors, and audit-log reads not tied to one feature."""

import json


def record(db, principal, source: str, token_label: str | None, operation: str,
           entity_type: str, entity_id: int, changes: dict) -> None:
    db.execute(
        """
        INSERT INTO audit_events
            (shop_id, actor_kind, actor_user_id, actor_username, operation, source,
             entity_type, entity_id, changes, token_label)
        VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (principal.shop_id, principal.user_id, principal.username, operation, source,
              entity_type, entity_id, json.dumps(changes), token_label),
    )


def newest_occurred_at(db):
    """Timestamp of the newest audit event, or None on an empty log."""
    return db.execute("SELECT MAX(occurred_at) FROM audit_events").fetchone()[0]
