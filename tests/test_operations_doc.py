"""Every `repairlib` command line quoted in the agent operations document is real."""

import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from test_baseline import python_env

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "repairlib-operations.md"
COMMAND = [sys.executable, "-m", "repairlib"]
GLOBAL_FLAGS = ("--json", "--dry-run")
EXAMPLE_IDS = {"$repair_id": "1", "$item_id": "1"}
ENV_ASSIGNMENT = r"[A-Za-z_][A-Za-z0-9_]*=\S+\s+"
LAUNCHER = re.compile(rf"^(?:{ENV_ASSIGNMENT})*(?:python3 -m repairlib|repairlib)(?=\s|$)(.*)$")
REQUIRED = (
    ("profile", "check"),
    ("customers", "create"),
    ("repairs", "create"),
    ("repairs", "add-charge"),
    ("repairs", "consume-part"),
    ("repairs", "pickup"),
    ("repairs", "print-label"),
)


def code_blocks(text):
    """Yield each fenced code block with backslash line continuations joined."""
    return (block.replace("\\\n", " ") for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S))


def quoted_commands(text):
    """Yield (line, leading positional tokens) for each quoted RepairLib invocation."""
    for block in code_blocks(text):
        for raw in block.splitlines():
            line = raw.strip()
            match = LAUNCHER.match(line)
            if not match:
                continue
            tokens = shlex.split(match.group(1))
            while tokens and tokens[0] in GLOBAL_FLAGS:
                tokens.pop(0)
            positional = []
            for token in tokens:
                if token.startswith("-"):
                    break
                positional.append(EXAMPLE_IDS.get(token, token))
            if not positional:
                raise AssertionError(f"Quoted RepairLib command has no subcommand: {line}")
            yield line, positional


class OperationsDocTests(unittest.TestCase):
    def test_document_exists_and_quotes_commands(self):
        self.assertTrue(DOC.is_file(), f"missing operations document: {DOC}")
        self.assertGreater(len(list(quoted_commands(DOC.read_text(encoding="utf-8")))), 5)

    def test_every_quoted_command_resolves_to_a_real_subcommand(self):
        for line, positional in quoted_commands(DOC.read_text(encoding="utf-8")):
            with self.subTest(command=" ".join(positional)):
                result = subprocess.run([*COMMAND, *positional, "--help"], capture_output=True,
                                        text=True, env=python_env(), timeout=60)
                self.assertEqual(result.returncode, 0,
                                 f"unknown command in operations doc: {line}\n{result.stderr}")

    def test_workflow_commands_are_documented_in_order(self):
        quoted = [tuple(positional[:2]) for _, positional in
                  quoted_commands(DOC.read_text(encoding="utf-8"))]
        positions = []
        for required in REQUIRED:
            self.assertIn(required, quoted, f"operations doc no longer shows `{' '.join(required)}`")
            positions.append(quoted.index(required))
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
