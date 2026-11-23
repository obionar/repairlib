"""AGENT-3 task read: one call gives an agent everything it needs to act on a repair."""

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest

from repairlib.adapters.mcp import tool_definitions
from repairlib.client import local
from test_baseline import python_env
from test_charges import PART
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import TokenFixture


class RepairBriefTests(TokenFixture, unittest.TestCase):
    def test_brief_combines_repair_customer_entries_charges_and_next_stages(self):
        ctx = local.open_workspace(self.workspace, "mcp")
        customer = local.create_customer(ctx, {"name": "Synthetic Brief", "phone": "+357 555 0101"}, "c1")
        repair = local.create_repair(ctx, {**DEVICE, "customer_id": customer.id}, "r1")
        for number in range(12):
            local.create_work_entry(ctx, repair.id, {"kind": "note", "body": f"Note {number}"}, f"e{number}")
        local.create_charge(ctx, repair.id, PART, "p1")
        repair = local.change_stage(ctx, repair.id, {"revision": local.show_repair(ctx, repair.id).revision,
                                                     "stage": "waiting_parts"}, "s1")

        brief = local.repair_brief(ctx, repair.id)
        self.assertEqual((brief.repair.id, brief.repair.revision, brief.repair.stage),
                         (repair.id, repair.revision, "waiting_parts"))
        # MCP is an agent route: the person stays private; a person's view shows them.
        self.assertEqual((brief.customer, brief.customer_phone), (f"C{customer.id}", "[masked]"))
        person = local.repair_brief(local.open_workspace(self.workspace, "cli"), repair.id)
        self.assertEqual((person.customer, person.customer_phone), ("Synthetic Brief", "+357 555 0101"))
        self.assertEqual(brief.next_stages, ["in_progress", "lab_test", "no_repair"])
        self.assertEqual([entry.body for entry in brief.recent_entries][:2], ["Note 11", "Note 10"])
        self.assertEqual(len(brief.recent_entries), 10)
        self.assertEqual(brief.charges.total_cents, local.charge_summary(ctx, repair.id).total_cents)
        self.assertIsNone(brief.charges.items[0].movement_id)  # consumption links stay out, as in charge_summary

        cli = subprocess.run([*COMMAND, "repairs", "brief", str(repair.id), "--workspace", str(self.workspace),
                              "--json"], capture_output=True, text=True, timeout=10, env=python_env())
        self.assertEqual(cli.returncode, 0, cli.stderr)
        payload = json.loads(cli.stdout)
        self.assertEqual((payload["repair"]["revision"], payload["next_stages"][0]), (repair.revision, "in_progress"))
        self.assertEqual(payload["charges"]["total_cents"], brief.charges.total_cents)
        self.assertIn("repair_brief", {tool.name for tool in tool_definitions()})

    def test_other_shop_repair_is_not_found(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO shops VALUES (2, 'Outside shop')")
            db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (50, 2, 'Outside repair')")
        with self.assertRaises(local.ClientError) as refused:
            local.repair_brief(local.open_workspace(self.workspace, "mcp"), 50)
        self.assertEqual(refused.exception.exit_code, 6)


if __name__ == "__main__":
    unittest.main()
