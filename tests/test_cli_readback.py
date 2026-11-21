"""P2 CLI readback: deleted markers, actionable refusals and a plain summary."""

import json
import subprocess
import unittest
from uuid import uuid4

from test_baseline import python_env
from test_charges import LABOR
from test_cli_identity import COMMAND, workspace_client
from test_inventory_adapters import API as INVENTORY, ITEM
from test_repairs import API, CUSTOMERS, DEVICE, USERS
from test_tokens import PASSWORD, TokenFixture
from test_users import ACCOUNT, issued_secret

GENERIC_CONFLICT = "conflicts with an existing change"


class CliReadbackTests(TokenFixture, unittest.TestCase):
    reuse_workspace = True

    def session(self, client):
        self.sign_in(client)
        token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
        return token, {"Authorization": f"Bearer {token}"}

    def api(self, client, auth, path, payload, status=201):
        response = client.post(path, headers={**auth, "Idempotency-Key": uuid4().hex}, json=payload)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def profile(self, client, token):
        path = self.workspace.parent / "profiles" / f"readback-{uuid4().hex}.json"
        return path

    def run_cli(self, profile, *args, command="repairs", code=0, machine=False):
        argv = [*COMMAND, command, *args, "--workspace", str(self.workspace), *(["--json"] if machine else [])]
        result = subprocess.run(argv, text=True, capture_output=True, timeout=15, env=python_env())
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def people(self, client, auth):
        customer = self.api(client, auth, CUSTOMERS, {"name": "Synthetic Readback"})
        technician = self.api(client, auth, USERS, {
            "username": "readback-tech", "role": "technician", "password": PASSWORD,
        })
        return customer, technician

    def test_deleted_entries_marked_in_plain_list(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.session(client)
            repair = self.api(client, auth, API, DEVICE)
            entry = self.api(client, auth, f"{API}/{repair['id']}/entries",
                             {"kind": "note", "body": "Synthetic removal note"})
            self.api(client, auth, f"{API}/{repair['id']}/entries/{entry['id']}/visibility",
                     {"revision": 1, "deleted": True, "reason": "Synthetic removal"}, status=200)
            profile = self.profile(client, token)

            plain = self.run_cli(profile, "entries", str(repair["id"]))
            row, = [line for line in plain.stdout.splitlines() if line.startswith(f"{entry['id']}\t")]
            self.assertIn("Synthetic removal note", row)
            self.assertTrue(row.endswith("(deleted)"), row)
            self.assertNotIn("deleted", self.run_cli(
                profile, "add-entry", str(repair["id"]), "--kind", "note",
                "--body", "Synthetic visible note", "--key", uuid4().hex,
            ).stdout)
            visible = self.run_cli(profile, "entries", str(repair["id"]))
            new_row = next(line for line in visible.stdout.splitlines() if "Synthetic visible note" in line)
            self.assertNotIn("(deleted)", new_row)

            machine = json.loads(self.run_cli(profile, "entries", str(repair["id"]), machine=True).stdout)
            self.assertEqual([(item["id"], item["deleted"]) for item in machine["items"]],
                             [(machine["items"][0]["id"], False), (entry["id"], True)])

    def test_conflict_codes_have_actionable_messages(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.session(client)
            profile = self.profile(client, token)
            repair = self.api(client, auth, API, DEVICE)
            route = f"{API}/{repair['id']}"

            invalid = self.run_cli(profile, "stage", str(repair["id"]), "--revision", "1",
                                   "--stage", "repaired", "--key", uuid4().hex, code=7)
            self.assertIn("not allowed", invalid.stderr)
            self.assertIn("current stage", invalid.stderr)
            self.assertNotIn(GENERIC_CONFLICT, invalid.stderr)
            self.assertEqual(client.get(route, headers=auth).json()["stage"], "new")

            item = self.api(client, auth, INVENTORY, ITEM)
            short = self.run_cli(profile, "consume-part", str(repair["id"]), str(item["id"]),
                                 "--repair-revision", "1", "--item-revision", "1",
                                 "--consumed-quantity", "1", "--billed-quantity", "1",
                                 "--key", uuid4().hex, code=7)
            self.assertIn("Not enough stock", short.stderr)
            self.assertNotIn(GENERIC_CONFLICT, short.stderr)
            self.assertEqual(client.get(f"{INVENTORY}/{item['id']}", headers=auth).json()["on_hand_milli"], 0)
            self.assertEqual(client.get(route, headers=auth).json()["revision"], 1)

            self.run_cli(profile, "archive", str(repair["id"]), "--revision", "1", "--key", uuid4().hex)
            archived = self.run_cli(profile, "stage", str(repair["id"]), "--revision", "2",
                                    "--stage", "in_progress", "--key", uuid4().hex, code=7)
            self.assertIn("archived", archived.stderr)
            self.assertIn("Restore", archived.stderr)
            self.assertNotIn(GENERIC_CONFLICT, archived.stderr)

            clash = self.run_cli(profile, "create", "--sku", ITEM["sku"], "--description", "Duplicate",
                                 "--selling-price", "1.00", "--key", uuid4().hex,
                                 command="inventory", code=7)
            self.assertIn("already exists", clash.stderr)
            self.assertNotIn(GENERIC_CONFLICT, clash.stderr)

    def test_plain_repairs_show_prints_summary(self):
        with workspace_client(self.workspace) as (client, _):
            token, auth = self.session(client)
            customer, technician = self.people(client, auth)
            repair = self.api(client, auth, API, {
                **DEVICE, "customer_id": customer["id"], "technician_id": technician["id"],
            })
            self.api(client, auth, f"{API}/{repair['id']}/charges", LABOR)
            profile = self.profile(client, token)

            plain = self.run_cli(profile, "show", str(repair["id"]))
            fields = dict(line.split("\t", 1) for line in plain.stdout.splitlines())
            self.assertEqual(fields["Repair"], str(repair["id"]))
            self.assertEqual(fields["Device"], f"{DEVICE['device_type']} {DEVICE['brand']} {DEVICE['model']}")
            self.assertEqual(fields["Customer"], "Synthetic Readback")
            self.assertEqual(fields["Technician"], "readback-tech")
            self.assertEqual(fields["Reported issue"], DEVICE["reported_issue"])
            self.assertEqual(fields["Stage"], "new")
            self.assertEqual(fields["Pickup"], "In shop")
            self.assertEqual(fields["Archive"], "Active")
            self.assertEqual(fields["Diagnosis"], "0.00 EUR")
            self.assertEqual(fields["Repair cost"], "10.00 EUR")
            self.assertEqual(fields["Total"], "10.00 EUR (not a payment)")
            self.assertIn("Revision\t", plain.stdout)

            machine = json.loads(self.run_cli(profile, "show", str(repair["id"]), machine=True).stdout)
            self.assertEqual(machine, repair)
