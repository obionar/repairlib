"""A credential that fails must be refused before it can take the database write lock."""

from contextlib import closing
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from repairlib.domain.customers import CustomerAccess, CustomerError, create_customer
from repairlib.domain.setup import initialize_workspace
from repairlib.contracts import CustomerInput


class AuthBeforeLockTests(unittest.TestCase):
    def test_unknown_token_is_refused_while_another_writer_holds_the_lock(self):
        with TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            initialize_workspace(workspace, shop="Synthetic", timezone="UTC",
                                 username="owner", password="synthetic lock gate passphrase")
            database = workspace / "state" / "repairlib.sqlite3"
            fake = CustomerAccess("api", "rl_" + secrets.token_urlsafe(32))
            with closing(sqlite3.connect(database)) as holder:
                holder.execute("BEGIN IMMEDIATE")
                with patch("repairlib.storage.sessions.SQLITE_BUSY_TIMEOUT_MS", 300):
                    started = time.monotonic()
                    with self.assertRaises(CustomerError) as refused:
                        create_customer(workspace, fake, CustomerInput(name="Nobody"), key="lock-gate-1")
                    elapsed = time.monotonic() - started
                holder.rollback()
        self.assertEqual(refused.exception.status, 401)
        self.assertLess(elapsed, 0.25)  # refused without waiting for the writer


if __name__ == "__main__":
    unittest.main()
