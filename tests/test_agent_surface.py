"""Small-model agent surface: a lean MCP tool set and CLI replies that say what happened."""

import json
import subprocess
import unittest

from repairlib.adapters.mcp import SHOP_TOOLS, repairlib_server
from repairlib.client import local
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import TokenFixture


class ShopToolProfileTests(unittest.TestCase):
    def test_shop_profile_lists_only_daily_tools_and_refuses_the_rest(self):
        server = repairlib_server(None, SHOP_TOOLS)
        self.assertEqual({tool.name for tool in server.repairlib_tools}, set(SHOP_TOOLS))
        refused = server.call_tool("edit_repair", {})
        self.assertEqual(refused["structuredContent"]["error"]["code"], "unknown_tool")
        self.assertGreater(len(repairlib_server(None).repairlib_tools), len(SHOP_TOOLS))


class CliReplyTests(TokenFixture, unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace)],
                              capture_output=True, text=True, timeout=10, env=python_env())

    def test_empty_list_says_so_on_stderr_and_attach_names_the_missing_file(self):
        repair = local.create_repair(local.open_workspace(self.workspace, "mcp"), DEVICE, "r1")
        empty = self.cli("list", "--query", "no such device")
        self.assertEqual((empty.returncode, empty.stdout, empty.stderr.strip()), (0, "", "No matches."))

        missing = self.cli("attach", str(repair.id), "/nowhere/photo 1.jpg", "--photo", "--key", "a1")
        self.assertEqual(missing.returncode, 8)
        self.assertIn("/nowhere/photo 1.jpg (No such file or directory)", missing.stderr)

        twice = self.cli("list", "--stage", "lab_test", "--stage", "customer_test")
        self.assertEqual(twice.returncode, 2)
        self.assertIn("give one --stage per command", twice.stderr)


class FewerCallsTests(TokenFixture, unittest.TestCase):
    """A write should need: find (list line carries the revision), then one write."""

    def setUp(self):
        super().setUp()
        self.ctx = local.open_workspace(self.workspace, "mcp")
        self.repair = local.create_repair(self.ctx, DEVICE, "r1")

    def cli(self, *args):
        return subprocess.run([*COMMAND, "repairs", *args, "--workspace", str(self.workspace)],
                              capture_output=True, text=True, timeout=10, env=python_env())

    def entries(self):
        return [entry.body for entry in local.list_work_entries(self.ctx, self.repair.id, limit=10, before=None).items]

    def test_list_line_carries_the_revision_a_write_needs(self):
        line = self.cli("list").stdout.strip().split("\t")
        self.assertEqual(line[5], f"r{self.repair.revision}")
        moved = self.cli("stage", str(self.repair.id), "--revision", line[5][1:], "--stage", "in_progress",
                         "--key", "s1")
        self.assertEqual(moved.returncode, 0, moved.stderr)

    def test_stage_with_note_writes_both_retries_safely_and_refusal_writes_nothing(self):
        args = ("stage", str(self.repair.id), "--revision", "1", "--stage", "in_progress",
                "--note", "Digitizer test: dead zone confirmed", "--key", "start-1")
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("revision 2, stage in_progress", first.stdout)
        self.assertEqual(self.cli(*args).returncode, 0)  # same key: both halves replay, nothing doubles
        self.assertEqual(self.entries(), ["Digitizer test: dead zone confirmed"])
        self.assertEqual(local.show_repair(self.ctx, self.repair.id).revision, 2)

        stale = self.cli("stage", str(self.repair.id), "--revision", "1", "--stage", "lab_test",
                         "--note", "should not appear", "--key", "stale-1")
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(self.entries(), ["Digitizer test: dead zone confirmed"])

    def test_mcp_stage_note_and_compact_list_rows(self):
        server = repairlib_server(self.ctx, SHOP_TOOLS)
        moved = server.call_tool("change_repair_stage", {"key": "m1", "repair_id": self.repair.id, "revision": 1,
                                                         "stage": "waiting_parts", "note": "97 Wh battery ordered"})
        self.assertNotIn("error", moved["structuredContent"])
        self.assertEqual(self.entries(), ["97 Wh battery ordered"])

        short = server.call_tool("list_repairs", {"queue": "open"})["structuredContent"]["result"]["items"]
        self.assertEqual(short, [{"id": self.repair.id, "device": "Laptop Synthetic Model Γ", "stage": "waiting_parts",
                                  "customer": "", "revision": 2, "paid": None, "collected": False,
                                  "archived": False}])
        full = repairlib_server(self.ctx).call_tool("list_repairs", {"queue": "open"})
        self.assertGreater(len(json.dumps(full)), 3 * len(json.dumps(short)))


if __name__ == "__main__":
    unittest.main()
