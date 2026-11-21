"""CLI JSON error envelope and the uniform revision flag."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]


class CliJsonTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "workspace"
        initialize_workspace(self.workspace, shop="Synthetic", timezone="UTC",
                             username="owner", password=PASSWORD)

    def run_cli(self, *args, code=0):
        result = subprocess.run(
            [*COMMAND, *args, "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=30)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def create_customer(self, name="Synthetic", key="create-key"):
        return self.run_cli("customers", "create", "--key", key, "--name", name)

    def repair(self):
        return json.loads(self.run_cli(
            "repairs", "create", "--key", "repair-key",
            "--reported-issue", "Synthetic issue", "--json").stdout)["id"]

    def test_json_failure_emits_one_object(self):
        self.create_customer()
        result = self.run_cli("--json", "customers", "edit", "1", "--revision", "2",
                              "--name", "Changed", "--key", "edit-key", code=7)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertEqual(set(error), {"code", "message", "fields"})
        self.assertEqual(error["code"], "stale_revision")
        self.assertTrue(error["message"])
        self.assertEqual(error["fields"], {})
        self.assertEqual(result.stderr, "")

    def test_json_success_keeps_payload(self):
        result = self.run_cli("--json", "customers", "create", "--key", "success-key",
                              "--name", "Synthetic")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "Synthetic")
        self.assertEqual(result.stderr, "")

    def test_non_json_failure_keeps_text(self):
        self.create_customer()
        result = self.run_cli("customers", "edit", "1", "--revision", "2",
                              "--name", "Changed", "--key", "edit-key", code=7)
        self.assertEqual(result.stdout, "")
        self.assertIn("customers: ", result.stderr)
        self.assertIn("changed after you reviewed", result.stderr)

    def test_mutation_accepts_revision_and_refuses_stale(self):
        repair_id = self.repair()
        stale = self.run_cli("--json", "repairs", "stage", str(repair_id), "--revision", "2",
                             "--stage", "in_progress", "--key", "stale-key", code=7)
        payload = json.loads(stale.stdout)
        self.assertEqual(payload["error"]["code"], "stale_revision")
        changed = self.run_cli("--json", "repairs", "stage", str(repair_id), "--revision", "1",
                               "--stage", "in_progress", "--key", "stage-key")
        self.assertEqual(json.loads(changed.stdout)["stage"], "in_progress")


if __name__ == "__main__":
    unittest.main()
