"""The shipped agent skill must describe the real CLI: every command, every link, real examples."""

import re
import unittest
from pathlib import Path

from repairlib.adapters import mcp as mcp_adapter
from repairlib.__main__ import build_parser
from repairlib.cli import catalog

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "repairlib" / "SKILL.md"
REFERENCES = SKILL.parent / "references"
LAUNCHER = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:repairlib|\"\$REPAIRLIB/bin/repairlib\"|"
                      r"\$repairlib|\"\$repairlib\")(?=\s|$)(.*)$")
PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_-]*$")


def cli_commands() -> dict[str, str]:
    """Top-level command -> its subcommands, from the generated catalog."""
    operations = catalog.build_catalog(build_parser(), mcp_adapter.tool_definitions())["operations"]
    commands: dict[str, set[str]] = {}
    for operation in operations:
        if operation["surface"] != "cli":
            continue
        tokens = operation["name"].split()
        commands.setdefault(tokens[0], set()).update(tokens[1:])
    return {name: " ".join(sorted(parts)) for name, parts in commands.items()}


def code_blocks(text: str):
    return (block.replace("\\\n", " ") for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S))


def quoted_commands(text: str):
    """Yield the positional tokens of each quoted RepairLib invocation."""
    for block in code_blocks(text):
        for raw in block.splitlines():
            line = raw.strip()
            match = LAUNCHER.match(line)
            if not match:
                continue
            tokens = match.group(1).split()
            positional = []
            for token in tokens:
                if token.startswith("-") or token in ("|", "&&", ">", "2>&1", "&", ";"):
                    break
                positional.append(token)
            if positional:
                yield line, positional


class SkillDocumentTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_frontmatter_names_the_skill_and_says_when_to_use_it(self):
        head = self.text.split("---", 2)[1]
        self.assertRegex(head, r"(?m)^name: repairlib$")
        self.assertRegex(head, r"(?m)^description: .{40,}")

    def test_every_relative_link_resolves(self):
        for target in re.findall(r"\]\(([^)#][^)]*)\)", self.text):
            if "://" in target:
                continue
            self.assertTrue((SKILL.parent / target).is_file(), f"broken link: {target}")

    def test_every_command_in_the_catalog_is_covered(self):
        commands = cli_commands()
        text = "\n".join(path.read_text(encoding="utf-8") for path in [SKILL, *REFERENCES.glob("*.md")])
        for name in commands:
            self.assertIn(name, text, f"the skill does not mention `{name}`")
        for name, subcommands in commands.items():
            for subcommand in subcommands.split():
                self.assertIn(subcommand, text, f"the skill does not document `{name} {subcommand}`")

    def test_quoted_examples_use_real_commands(self):
        commands = cli_commands()
        for line, positional in quoted_commands(self.text):
            self.assertIn(positional[0], commands, f"unknown command in example: {line}")
            if len(positional) > 1 and not PLACEHOLDER.match(positional[1]):
                subcommands = commands[positional[0]].split()
                self.assertIn(positional[1], subcommands, f"unknown subcommand in example: {line}")

    def test_references_quote_real_commands_too(self):
        commands = cli_commands()
        for path in sorted(REFERENCES.glob("*.md")):
            for line, positional in quoted_commands(path.read_text(encoding="utf-8")):
                self.assertIn(positional[0], commands, f"unknown command in {path.name}: {line}")
                if len(positional) > 1 and not PLACEHOLDER.match(positional[1]):
                    self.assertIn(positional[1], commands[positional[0]].split(),
                                  f"unknown subcommand in {path.name}: {line}")

    def test_the_skill_states_the_two_hard_refusals(self):
        self.assertIn("No direct SQL", self.text)
        self.assertIn("No service control", self.text)


if __name__ == "__main__":
    unittest.main()
