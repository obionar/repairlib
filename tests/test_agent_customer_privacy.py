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

    def test_agent_cli_lists_minimal_and_answers_requests_about_a_person(self):
        minimal = [self.cli(*args) for args in (
            ("repairs", "list"), ("repairs", "list", "--json"), ("repairs", "list", "--query", "maria"),
            ("repairs", "brief", str(self.repair.id)), ("repairs", "show", str(self.repair.id)),
            ("customers", "list"))]
        for text in minimal:
            self.assertPrivate(text)
        self.assertPrivate(self.cli("dashboard"), shows_customer=False)  # the dashboard text lists no customers

        # "Tell me all repairs from Maria": search the name, take the ID, list repairs by customer ID.
        found = self.cli("customers", "list", "--query", "maria")
        self.assertIn(NAME, found)
        self.assertNotIn(PHONE, found)
        by_customer = self.cli("repairs", "list", "--customer", str(self.person.id))
        self.assertIn(f"{self.repair.id}\t", by_customer)
        # "Whose is repair 1?" / "Maria's phone?": one customer's record is the explicit request.
        record = self.cli("customers", "show", str(self.person.id))
        self.assertIn(NAME, record)
        self.assertIn(PHONE, record)
        self.assertIn(NAME, self.cli("repairs", "list", agent=False))  # a person sees names everywhere

    def test_mcp_lists_minimal_and_answers_requests_about_a_person(self):
        server = repairlib_server(local.open_workspace(self.workspace, "mcp"))

        def tool(name, **args):
            return server.call_tool(name, args)["structuredContent"]["result"]
        minimal = [tool("list_repairs", query="maria"), tool("repair_brief", repair_id=self.repair.id),
                   tool("list_customers"), tool("dashboard")]
        self.assertPrivate(json.dumps(minimal))
        self.assertEqual(minimal[0]["items"][0]["id"], self.repair.id)
        found = tool("list_customers", query="maria")["items"][0]
        self.assertEqual((found["name"], found["phone"]), (NAME, "[masked]"))
        record = tool("get_customer", customer_id=self.person.id)
        self.assertEqual((record["name"], record["phone"], record["email"]), (NAME, PHONE, EMAIL))

    def test_agent_api_token_lists_minimal_user_token_sees_everything(self):
        with self.client() as client:
            self.sign_in(client)
            auth = {}
            for kind in ("agent", "user"):
                issued = client.post(ACCOUNT, data=self.issue_form(client, name=f"T {kind}", kind=kind))
                self.assertEqual(issued.status_code, 201)
                auth[kind] = {"Authorization": f"Bearer {issued_secret(issued)}"}

            def get(kind, path):
                return client.get(path, headers=auth[kind]).json()
            lists = ("/api/v1/repairs", "/api/v1/dashboard", "/api/v1/customers")
            self.assertPrivate(json.dumps([get("agent", path) for path in lists]))
            found = get("agent", "/api/v1/customers?q=maria")["items"][0]
            record = get("agent", f"/api/v1/customers/{self.person.id}")
            for value in (found, record):  # names on request; agent tokens keep contacts masked
                self.assertEqual((value["name"], value["phone"]), (NAME, "[masked]"))
            everything = json.dumps([get("user", path) for path in lists])
            self.assertIn(NAME, everything)
            self.assertIn(PHONE, everything)

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
