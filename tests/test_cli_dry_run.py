"""Global `--dry-run`: covered mutations report their effect and leave the workspace byte-identical."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_tokens import PASSWORD

COMMAND = [sys.executable, "-m", "repairlib"]


class DryRunTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "workspace"
        initialize_workspace(self.workspace, shop="Synthetic", timezone="UTC",
                             username="owner", password=PASSWORD)

    def cli(self, *args, code=0):
        result = subprocess.run(
            [*COMMAND, *args, "--workspace", str(self.workspace)],
            capture_output=True, text=True, env=python_env(), timeout=60)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def snapshot(self):
        """Every file under the workspace, by relative path and bytes.

        SQLite's `-shm` index and an empty `-wal` appear whenever any connection opens a WAL
        database, even for a read; a non-empty `-wal` would mean frames were written, so it counts.
        """
        files = {}
        for path in sorted(self.workspace.rglob("*")):
            data = path.read_bytes() if path.is_file() else None
            if data is None or path.name.endswith("-shm") or (path.name.endswith("-wal") and not data):
                continue
            files[str(path.relative_to(self.workspace))] = data
        return files

    def counts(self):
        database = next(self.workspace.rglob("*.sqlite3"), None) or next(self.workspace.rglob("*.db"))
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = [name for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND (name = 'audit_events' "
                "OR name LIKE '%\\_requests' ESCAPE '\\')")]
            return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}

    def assert_untouched(self, *args, expect, code=0):
        before, counts = self.snapshot(), self.counts()
        result = self.cli("--dry-run", *args, code=code)
        self.assertEqual(self.snapshot(), before, "workspace bytes changed")
        self.assertEqual(self.counts(), counts, "a row, audit event or receipt was written")
        for text in expect:
            self.assertIn(text, result.stdout)
        return result

    def real(self, *args):
        return json.loads(self.cli(*args, "--json").stdout)

    def repair(self):
        return self.real("repairs", "create", "--key", "seed-repair", "--reported-issue", "Synthetic issue")

    def test_customer_create_on_a_fresh_workspace(self):
        # No built-in `cli` actor exists yet: even creating it must not persist.
        self.assert_untouched("customers", "create", "--key", "k1", "--name", "Synthetic Person",
                              expect=["Dry run", "customer.create", "new record", "name"])
        self.assertEqual(self.real("customers", "list")["items"], [])

    def test_repair_create(self):
        self.real("customers", "create", "--key", "seed-customer", "--name", "Synthetic")
        self.assert_untouched("repairs", "create", "--key", "k2", "--reported-issue", "Synthetic issue",
                              expect=["Dry run", "repair.create", "new record", "reported_issue"])
        self.assertEqual(self.real("repairs", "list")["items"], [])

    def test_stage_change(self):
        repair = self.repair()
        self.assert_untouched("repairs", "stage", str(repair["id"]), "--revision", str(repair["revision"]),
                              "--stage", "in_progress", "--key", "k3",
                              expect=["Dry run", "repair.stage", f"repair {repair['id']}", "stage"])
        self.assertEqual(self.real("repairs", "show", str(repair["id"]))["stage"], "new")

    def test_charge_create(self):
        repair = self.repair()
        self.assert_untouched("repairs", "add-charge", str(repair["id"]), "--kind", "labor",
                              "--description", "Synthetic labor", "--quantity", "1",
                              "--unit-price", "10.00", "--key", "k4",
                              expect=["Dry run", "charge.create", "new record"])

    def test_pickup(self):
        repair = self.repair()
        for stage in ("in_progress", "repaired"):
            repair = self.real("repairs", "stage", str(repair["id"]), "--revision", str(repair["revision"]),
                               "--stage", stage, "--key", f"to-{stage}")
        self.assert_untouched("repairs", "pickup", str(repair["id"]), "--revision", str(repair["revision"]),
                              "--key", "k5", expect=["Dry run", "repair.pickup", f"repair {repair['id']}"])
        self.assertIsNone(self.real("repairs", "show", str(repair["id"]))["picked_up_at"])

    def test_json_report_and_key_is_not_consumed(self):
        before = self.snapshot()
        result = self.cli("--dry-run", "--json", "customers", "create", "--key", "reuse", "--name", "Synthetic")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["events"][0]["operation"], "customer.create")
        self.assertIsNone(payload["events"][0]["entity_id"])
        self.assertEqual(self.snapshot(), before)
        # The same key still creates for real afterwards: no receipt was stored.
        self.assertEqual(self.real("customers", "create", "--key", "reuse", "--name", "Synthetic")["id"], 1)

    def test_validation_refusal_is_unchanged_and_writes_nothing(self):
        before = self.snapshot()
        result = self.cli("--dry-run", "customers", "create", "--key", "k6", "--name", "", code=8)
        self.assertNotIn("Dry run", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_uncovered_mutation_refuses(self):
        before = self.snapshot()
        result = self.cli("--dry-run", "customers", "archive", "1", "--revision", "1", "--key", "k7", code=2)
        self.assertIn("--dry-run does not support", result.stderr)
        self.assertEqual(self.snapshot(), before)
        envelope = json.loads(self.cli("--dry-run", "--json", "customers", "archive", "1", "--revision", "1",
                                       "--key", "k7", code=2).stdout)
        self.assertEqual(set(envelope), {"error"})
        self.assertEqual(envelope["error"]["code"], "dry_run_unsupported")

    def test_read_command_and_top_level_command_refuse(self):
        for command in (("customers", "list"), ("cleanup",)):
            self.assertIn("--dry-run does not support", self.cli("--dry-run", *command, code=2).stderr)

    def test_without_the_flag_behaviour_is_unchanged(self):
        before = self.counts()["audit_events"]
        result = self.cli("customers", "create", "--key", "k8", "--name", "Synthetic Person")
        self.assertEqual(result.stdout, "Created customer 1.\n")
        self.assertEqual(self.counts()["audit_events"], before + 2)  # local actor + customer.create


if __name__ == "__main__":
    unittest.main()
