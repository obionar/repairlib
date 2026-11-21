"""The operation catalog is generated from argparse and the MCP registry, and a drift gate keeps it honest."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


from repairlib.__main__ import build_parser
from repairlib.adapters import mcp as mcp_adapter
from repairlib.cli import catalog
from test_baseline import python_env

COMMAND = [sys.executable, "-m", "repairlib"]
MCP_SOURCE = Path(mcp_adapter.__file__).read_text(encoding="utf-8")


def subparsers_of(parser):
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def parser_paths(parser, prefix=()):
    """Every subcommand path that can be run: leaves and groups whose subcommand is optional."""
    found = set()
    group = subparsers_of(parser)
    if group is None:
        return found
    for name, child in group.choices.items():
        path = (*prefix, name)
        nested = subparsers_of(child)
        if nested is None or not nested.required:
            found.add(path)
        found |= parser_paths(child, path)
    return found


def names(data, surface):
    return {entry["name"] for entry in data["operations"] if entry["surface"] == surface}


def source_tool_names():
    return set(re.findall(r'register(?:_mutation)?\(\s*"([a-z_]+)"', MCP_SOURCE))


class OperationCatalogTests(unittest.TestCase):
    def build(self, parser=None, tools=None):
        return catalog.build_catalog(parser or build_parser(),
                                     mcp_adapter.tool_definitions() if tools is None else tools)

    def test_contains_every_subcommand_and_every_mcp_tool_and_nothing_else(self):
        data = self.build()
        self.assertEqual(names(data, "cli"), {" ".join(path) for path in parser_paths(build_parser())})
        self.assertEqual(names(data, "mcp"), source_tool_names())
        self.assertEqual(names(data, "mcp"), {tool.name for tool in mcp_adapter.tool_definitions()})
        self.assertTrue({"customers create", "repairs stage", "catalog"} <= names(data, "cli"))
        self.assertTrue({"get_repair", "list_customers"} <= names(data, "mcp"))

    def test_drift_gate_fails_when_a_command_or_tool_is_added_without_appearing(self):
        parser = build_parser()
        fake = subparsers_of(parser).add_parser("fake-op", help="Synthetic command")
        fake.add_argument("--flavor", required=True, help="Synthetic flavor")
        tool = mcp_adapter.Tool(name="fake_tool", description="Synthetic tool", input_schema={
            "type": "object", "properties": {"size": {"type": "integer", "description": "Synthetic size"}},
            "required": ["size"]})
        data = self.build(parser, [*mcp_adapter.tool_definitions(), tool])
        # The catalog follows the registries: the new entries appear with their arguments.
        self.assertIn("fake-op", names(data, "cli"))
        self.assertIn("fake_tool", names(data, "mcp"))
        flavor = next(e for e in data["operations"] if e["name"] == "fake-op")["arguments"][0]
        self.assertEqual((flavor["name"], flavor["required"], flavor["help"]),
                         ("--flavor", True, "Synthetic flavor"))
        # The gate's independent enumeration of the real code does not know the fakes, so a catalog
        # that failed to list one would differ from what the gate expects.
        self.assertEqual(names(data, "cli"), {" ".join(p) for p in parser_paths(parser)})
        self.assertNotEqual(names(data, "cli"), names(self.build(), "cli"))
        self.assertNotEqual(names(data, "mcp"), source_tool_names())

    def test_entries_carry_argument_type_required_and_help(self):
        entries = {(e["surface"], e["name"]): e for e in self.build()["operations"]}
        arguments = entries[("cli", "repairs stage")]["arguments"]
        self.assertTrue(arguments)
        for argument in arguments:
            self.assertIn(argument["type"], {"string", "integer", "boolean", "number"})
            self.assertIsInstance(argument["required"], bool)
            self.assertIn("help", argument)
        key = next(a for a in entries[("cli", "customers create")]["arguments"] if a["name"] == "--key")
        self.assertTrue(key["help"] or key["required"])
        tool = {a["name"]: a for a in entries[("mcp", "get_repair")]["arguments"]}
        self.assertEqual(tool["repair_id"]["type"], "integer")
        self.assertTrue(tool["repair_id"]["required"])
        self.assertTrue(entries[("mcp", "get_repair")]["description"])
        self.assertTrue(entries[("mcp", "get_repair")]["read_only"])
        self.assertFalse(entries[("mcp", "purge_attachment")]["read_only"])

    def test_json_output_is_stable_sorted_and_free_of_paths(self):
        first = subprocess.run([*COMMAND, "catalog", "--json"], capture_output=True, text=True,
                               env=python_env(), timeout=60)
        second = subprocess.run([*COMMAND, "--json", "catalog"], capture_output=True, text=True,
                                env=python_env(), timeout=60)
        self.assertEqual((first.returncode, second.returncode), (0, 0), (first.stderr, second.stderr))
        self.assertEqual(first.stdout, second.stdout)
        data = json.loads(first.stdout)
        keys = [(e["surface"], e["name"]) for e in data["operations"]]
        self.assertEqual(keys, sorted(keys))
        for path in (str(Path.home()), str(Path(__file__).resolve().parents[1])):
            self.assertNotIn(path, first.stdout)
        self.assertEqual(data, self.build())

    def test_text_output_is_one_line_per_operation(self):
        result = subprocess.run([*COMMAND, "catalog"], capture_output=True, text=True,
                                env=python_env(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), len(self.build()["operations"]))
        fields = [line.split("\t") for line in lines]
        self.assertIn(["cli", "repairs stage"], [f[:2] for f in fields])
        mcp_line = next(f for f in fields if f[:2] == ["mcp", "get_repair"])
        self.assertIn("repair_id", mcp_line[2])


if __name__ == "__main__":
    unittest.main()
