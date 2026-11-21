"""The stdlib MCP server answers JSON-RPC framing correctly without the SDK or a workspace."""

import io
import json
import unittest

from repairlib.adapters.mcp import repairlib_server


def run(lines):
    sink = io.StringIO()
    repairlib_server(None).serve(io.StringIO("".join(line + "\n" for line in lines)), sink)
    return [json.loads(line) for line in sink.getvalue().splitlines()]


class ProtocolTests(unittest.TestCase):
    def test_handshake_ping_and_listing(self):
        replies = run([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
        ])
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "RepairLib")
        self.assertEqual(replies[1]["result"], {})
        names = {tool["name"] for tool in replies[2]["result"]["tools"]}
        self.assertTrue({"get_repair", "list_customers"} <= names)

    def test_unsupported_version_falls_back_to_a_supported_one(self):
        reply, = run([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "1999-01-01"}})])
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")

    def test_malformed_input_unknown_method_and_unknown_tool_are_refused(self):
        replies = run([
            "not json",
            json.dumps([1, 2]),
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "resources/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": 5}}),
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}}),
            json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                        "params": {"name": "get_repair", "arguments": {"repair_id": "x"}}}),
        ])
        self.assertEqual([reply.get("error", {}).get("code") for reply in replies[:4]],
                         [-32700, -32600, -32601, -32602])
        self.assertEqual(replies[4]["result"]["structuredContent"]["error"]["code"], "unknown_tool")
        self.assertEqual(replies[5]["result"]["structuredContent"]["error"]["code"], "validation")
        self.assertTrue(replies[5]["result"]["isError"])

    def test_an_unexpected_tool_failure_is_reported_and_the_session_continues(self):
        # With no workspace context every operation raises AttributeError, standing in for a bug.
        replies = run([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "list_customers", "arguments": {}}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "archive_repair",
                                   "arguments": {"repair_id": 1, "revision": 1, "key": "k1"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        ])
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        read, write = (reply["result"] for reply in replies[:2])
        for result in (read, write):
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "server_error")
            self.assertNotIn("AttributeError", json.dumps(result))
        self.assertNotIn("uncertain", read["structuredContent"]["error"]["message"])
        self.assertIn("uncertain", write["structuredContent"]["error"]["message"])
        self.assertEqual(replies[2]["result"], {})


if __name__ == "__main__":
    unittest.main()
