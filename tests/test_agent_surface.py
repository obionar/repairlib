"""Small-model agent surface: a lean MCP tool set and CLI replies that say what happened."""

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


if __name__ == "__main__":
    unittest.main()
