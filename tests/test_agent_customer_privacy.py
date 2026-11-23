"""Customer privacy from AI agents: names become `C<id>`, contacts `[masked]`, on every agent route.

Agent routes: MCP, agent API tokens, and the CLI when `REPAIRLIB_AGENT=1` is set. People (web,
person tokens, the plain CLI) keep seeing names and contacts. Search by name still works.
"""

import json
import os
import subprocess
import unittest
from uuid import uuid4

from repairlib.adapters.mcp import repairlib_server
from repairlib.client import local
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret

NAME, PHONE, EMAIL, ADDRESS = "Maria Synthetic", "+35799000111", "maria@example.test", "1 Synthetic St"
SECRETS = (NAME, "Maria", PHONE, "99000111", EMAIL, ADDRESS)


class CustomerPrivacyTests(TokenFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = local.open_workspace(self.workspace, "cli")
        self.person = local.create_customer(self.ctx, {"name": NAME, "phone": PHONE, "email": EMAIL,
                                                       "address": ADDRESS}, "c1")
        self.repair = local.create_repair(self.ctx, {**DEVICE, "customer_id": self.person.id}, "r1")
        self.ref = f"C{self.person.id}"

    def cli(self, *args, agent=True):
        env = python_env()
        env.pop("REPAIRLIB_AGENT", None)
        if agent:
            env["REPAIRLIB_AGENT"] = "1"
        done = subprocess.run([*COMMAND, *args, "--workspace", str(self.workspace)],
                              capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def assertPrivate(self, text, shows_customer=True):
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        if shows_customer:
            self.assertIn(self.ref, text)

    def test_agent_cli_shows_customer_ids_never_names_or_contacts(self):
        outputs = [self.cli(*args) for args in (
            ("repairs", "list"), ("repairs", "list", "--json"), ("repairs", "list", "--query", "maria"),
            ("repairs", "brief", str(self.repair.id)), ("repairs", "show", str(self.repair.id)),
            ("customers", "list"), ("customers", "show", str(self.person.id)),
            ("customers", "show", str(self.person.id), "--json"), ("dashboard",))]
        for text in outputs[:-1]:
            self.assertPrivate(text)
        self.assertPrivate(outputs[-1], shows_customer=False)  # the dashboard text lists no customers
        self.assertIn(str(self.repair.id), outputs[2])  # searching by the name still finds the repair
        self.assertIn(NAME, self.cli("repairs", "list", agent=False))  # a person still sees the name
        self.assertEqual(local.show_customer(self.ctx, self.person.id).name, NAME)

    def test_mcp_shows_customer_ids_never_names_or_contacts(self):
        server = repairlib_server(local.open_workspace(self.workspace, "mcp"))
        results = [server.call_tool(name, args)["structuredContent"]["result"] for name, args in (
            ("list_repairs", {"query": "maria"}), ("repair_brief", {"repair_id": self.repair.id}),
            ("list_customers", {}), ("get_customer", {"customer_id": self.person.id}), ("dashboard", {}))]
        self.assertPrivate(json.dumps(results))
        self.assertEqual(results[0]["items"][0]["id"], self.repair.id)

    def test_agent_api_token_shows_customer_ids_user_token_shows_names(self):
        with self.client() as client:
            self.sign_in(client)
            seen = {}
            for kind in ("agent", "user"):
                issued = client.post(ACCOUNT, data=self.issue_form(client, name=f"T {kind}", kind=kind))
                self.assertEqual(issued.status_code, 201)
                auth = {"Authorization": f"Bearer {issued_secret(issued)}"}
                seen[kind] = json.dumps([client.get(path, headers=auth).json() for path in (
                    "/api/v1/repairs", "/api/v1/dashboard", "/api/v1/customers",
                    f"/api/v1/customers/{self.person.id}")])
            self.assertPrivate(seen["agent"])
            self.assertIn(NAME, seen["user"])
            self.assertIn(PHONE, seen["user"])

    def test_staff_see_the_customer_id_agents_use_in_the_web_list_and_record(self):
        with self.client() as client:
            self.sign_in(client)
            listing = client.get("/customers").text
            record = client.get(f"/customers/{self.person.id}").text
        self.assertIn(f">{self.ref}<", listing)
        self.assertIn(NAME, listing)
        self.assertIn(f">{self.ref}<", record)
        self.assertIn("Customer ID", record)


if __name__ == "__main__":
    unittest.main()
