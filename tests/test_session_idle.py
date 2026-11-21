"""Server-side idle expiry and cleanup pruning."""

from contextlib import closing
import sqlite3
import time
import unittest
from datetime import UTC, datetime

from repairlib.lifecycle.operations import cleanup_workspace
from test_tokens import TokenFixture

IDLE_SECONDS = 60 * 60
RETENTION_SECONDS = 90 * 24 * 60 * 60


def stamp(seconds_ago: int) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, UTC).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class SessionIdleTests(TokenFixture, unittest.TestCase):
    def age_session(self, seconds: int) -> None:
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET last_seen_at = ?", (int(time.time()) - seconds,))

    def test_idle_session_is_refused_and_activity_extends_it(self):
        with self.client() as client:
            self.sign_in(client)
            self.age_session(IDLE_SECONDS + 1)
            refused = client.get("/repairs")
            self.assertEqual((refused.status_code, refused.headers["location"]), (303, "/login"))
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM sessions"), [(1,)])
        with self.client() as client:
            self.sign_in(client)
            self.age_session(IDLE_SECONDS - 60)
            self.assertEqual(client.get("/repairs").status_code, 200)
            seen = self.snapshot("SELECT max(last_seen_at) FROM sessions")[0][0]
            self.assertLess(abs(seen - int(time.time())), 5)
            self.age_session(IDLE_SECONDS - 60)
            self.assertEqual(client.get("/repairs").status_code, 200)

    def test_cleanup_prunes_expired_sessions_and_old_receipts(self):
        now = int(time.time())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Idle customer')")
            customer = db.execute("SELECT id FROM customers").fetchone()[0]
            for token, expires, seen in (("expired", now - 1, now - 1), ("live", now + 600, now)):
                db.execute(
                    "INSERT INTO sessions(token_hash, shop_id, user_id, user_revision, expires_at, last_seen_at) "
                    "SELECT ?, shop_id, id, revision, ?, ? FROM users WHERE username = 'owner'",
                    (token, expires, seen),
                )
            for key, created in (("old", stamp(RETENTION_SECONDS + 60)), ("new", stamp(60))):
                db.execute(
                    "INSERT INTO customer_requests(shop_id, actor_id, operation, request_key, fingerprint, "
                    "customer_id, result, created_at) "
                    "SELECT shop_id, id, 'customer.create', ?, 'fingerprint', ?, '{}', ? "
                    "FROM users WHERE username = 'owner'",
                    (key, customer, created),
                )
        report = cleanup_workspace(self.workspace)
        self.assertEqual((report["sessions"], report["receipts"]), (1, 1))
        self.assertEqual(self.snapshot("SELECT token_hash FROM sessions"), [("live",)])
        self.assertEqual(self.snapshot("SELECT request_key FROM customer_requests"), [("new",)])


if __name__ == "__main__":
    unittest.main()
