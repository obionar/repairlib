"""The repair brief shows live notes only, chosen before its ten-entry limit (audit R14)."""

import subprocess
import unittest

from repairlib.client import local
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import TokenFixture


class BriefNoteTests(TokenFixture, unittest.TestCase):
    def test_deleted_notes_are_absent_and_do_not_displace_an_older_live_note(self):
        ctx = local.open_workspace(self.workspace)
        repair = local.create_repair(ctx, DEVICE, "repair").id
        live = local.create_work_entry(ctx, repair, {"kind": "note", "body": "Older live advice"}, "live")
        for number in range(10):
            entry = local.create_work_entry(ctx, repair, {"kind": "note", "body": f"Superseded {number}"},
                                            f"gone-{number}")
            local.change_entry_visibility(ctx, repair, entry.id, {"revision": entry.revision, "deleted": True,
                                                                   "reason": "Withdrawn"}, f"del-{number}")
        self.assertEqual([e.id for e in local.repair_brief(ctx, repair).recent_entries], [live.id])
        done = subprocess.run([*COMMAND, "repairs", "brief", str(repair), "--workspace", str(self.workspace)],
                              text=True, capture_output=True, timeout=30, env=python_env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Older live advice", done.stdout)
        self.assertNotIn("Superseded", done.stdout)
        # History keeps the withdrawn entries.
        self.assertEqual(len(local.list_work_entries(ctx, repair, limit=25, before=None).items), 11)


if __name__ == "__main__":
    unittest.main()
