"""A short exclusive check (status, profile check) must not refuse a parallel CLI or MCP call."""

import threading
import time
import unittest

from repairlib.client import local
from repairlib.lifecycle.workspace import maintenance_lock
from test_tokens import TokenFixture


class LocalLockWaitTests(TokenFixture, unittest.TestCase):
    def hold(self, seconds):
        held = threading.Event()

        def run():
            with maintenance_lock(self.workspace, exclusive=True, create=False):
                held.set()
                time.sleep(seconds)
        thread = threading.Thread(target=run)
        thread.start()
        held.wait()
        return thread

    def test_read_waits_for_a_brief_exclusive_holder(self):
        ctx = local.open_workspace(self.workspace, "mcp")
        thread = self.hold(0.5)
        self.assertEqual(local.list_repairs(ctx, limit=5, before=None, query="", stage=None, picked_up=None,
                                            archived=None, technician="", customer_id=None, queue="",
                                            paid=None).items, [])
        thread.join()

    def test_long_maintenance_still_answers_busy(self):
        ctx = local.open_workspace(self.workspace, "mcp")
        thread = self.hold(local.LOCK_WAIT_SECONDS + 1)
        started = time.monotonic()
        with self.assertRaises(local.ClientError) as refused:
            local.read_dashboard(ctx)
        self.assertEqual(refused.exception.code, "busy")
        self.assertGreaterEqual(time.monotonic() - started, local.LOCK_WAIT_SECONDS - 0.2)
        thread.join()


if __name__ == "__main__":
    unittest.main()
