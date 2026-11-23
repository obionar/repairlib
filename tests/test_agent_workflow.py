"""Agent write rules judged by state and audit events, not prose (audit addendum)."""

from contextlib import closing
import sqlite3
import unittest

from repairlib.client import local
from test_repairs import DEVICE
from test_tokens import TokenFixture

ENTRY = {"kind": "note", "body": "Replaced the fuse"}


class AgentWorkflowTests(TokenFixture, unittest.TestCase):
    def count(self, sql, *args):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql, args).fetchone()[0]

    def test_replay_in_the_same_adapter_is_one_effect_and_switching_adapter_is_a_new_write(self):
        cli = local.open_workspace(self.workspace, actor="cli")
        mcp = local.open_workspace(self.workspace, actor="mcp")
        repair = local.create_repair(cli, DEVICE, "repair").id
        first = local.create_work_entry(cli, repair, ENTRY, "note-1")
        self.assertEqual(local.create_work_entry(cli, repair, ENTRY, "note-1").id, first.id)
        self.assertEqual(self.count("SELECT count(*) FROM work_entries"), 1)
        self.assertEqual(self.count("SELECT count(*) FROM audit_events WHERE operation = 'work_entry.create'"), 1)
        # A different actor is a different writer: the same key is a second note, attributed to `mcp`.
        second = local.create_work_entry(mcp, repair, ENTRY, "note-1")
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(self.count("SELECT count(*) FROM work_entries"), 2)

    def test_stale_revision_changes_nothing_and_a_lost_stage_and_note_reply_replays_once(self):
        ctx = local.open_workspace(self.workspace)
        repair = local.create_repair(ctx, DEVICE, "repair").id
        change = {"revision": 1, "stage": "in_progress"}
        done = local.change_stage_with_note(ctx, repair, change, "stage-1", "Started")
        self.assertEqual(local.change_stage_with_note(ctx, repair, change, "stage-1", "Started"), done)
        before = (self.count("SELECT count(*) FROM work_entries"), self.count("SELECT count(*) FROM audit_events"))
        self.assertEqual(before[0], 1)
        with self.assertRaises(local.ClientError) as stale:
            local.change_stage_with_note(ctx, repair, {"revision": 1, "stage": "waiting_parts"}, "stage-2", "Late")
        self.assertEqual(stale.exception.code, "stale_revision")
        self.assertEqual((self.count("SELECT count(*) FROM work_entries"),
                          self.count("SELECT count(*) FROM audit_events")), before)

    def test_ambiguous_search_matches_are_listed_without_any_write(self):
        ctx = local.open_workspace(self.workspace)
        for number in range(2):
            local.create_repair(ctx, DEVICE | {"serial": f"SN-{number}"}, f"repair-{number}")
        audits = self.count("SELECT count(*) FROM audit_events")
        page = local.list_repairs(ctx, limit=10, before=None, query="Synthetic")
        self.assertEqual(len(page.items), 2)
        self.assertEqual(self.count("SELECT count(*) FROM audit_events"), audits)


if __name__ == "__main__":
    unittest.main()
