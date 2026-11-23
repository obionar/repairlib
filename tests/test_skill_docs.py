"""The shipped agent skill covers the real CLI and runs its synthetic example."""

import json
import os
import re
from secrets import token_urlsafe
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from repairlib.adapters import mcp as mcp_adapter
from repairlib.__main__ import build_parser
from repairlib.cli import catalog
from test_baseline import python_env
from test_cli_identity import COMMAND

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
        # A plain scalar cannot hold ": "; the harness YAML parse rejects it.
        for line in head.splitlines():
            key, _, value = line.partition(": ")
            if value and not value.startswith(('"', "'", "|", ">", "[")):
                self.assertNotIn(": ", value, f"quote the frontmatter {key} value")

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

    def test_agents_snippet_matches_the_skill_shop_section(self):
        block, = code_blocks((REFERENCES / "agents-snippet.md").read_text(encoding="utf-8"))
        shop = block.split("\n\n", 1)[1]  # after the snippet's own two-line preface
        self.assertIn(shop.strip(), self.text, "agents-snippet.md and SKILL.md 'Shop tasks' differ")

    def test_the_skill_states_the_two_hard_refusals(self):
        self.assertIn("No direct SQL", self.text)
        self.assertIn("No service control", self.text)

    @unittest.skipUnless(shutil.which("sh") and shutil.which("jq"), "shell example needs sh and jq")
    def test_quick_workflow_uses_returned_ids_and_chained_revisions(self):
        script, = code_blocks((REFERENCES / "quick-workflow.md").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temporary:
            env = dict(os.environ, TMPDIR=temporary, DEMO_PASSWORD=token_urlsafe(24),
                       PATH=f"{ROOT / 'bin'}{os.pathsep}{os.environ['PATH']}")
            env.pop("REPAIRLIB_BASE_URL", None)
            result = subprocess.run(["sh", "-c", script], cwd=temporary, env=env,
                                    capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertGreater(record["customer_id"], 3)
        self.assertEqual(record["stage"], "repaired")
        self.assertTrue(record["payment_paid"])
        self.assertIsNotNone(record["picked_up_at"])


if __name__ == "__main__":
    unittest.main()


class UnpaidRecipeTests(unittest.TestCase):
    """The documented 'all finished and unpaid' recipe finds collected repairs too (audit R15)."""

    def test_recipe_lists_collected_and_uncollected_unpaid_but_not_paid_repairs(self):
        from repairlib.client import local
        from test_repairs import DEVICE
        from test_tokens import TokenFixture

        class Fixture(TokenFixture, unittest.TestCase):
            def runTest(self):
                pass

        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ctx = local.open_workspace(fixture.workspace)

        def finish(repair, stage, *, picked_up, paid):
            revision = local.show_repair(ctx, repair).revision
            local.change_stage(ctx, repair, {"revision": revision, "stage": stage}, f"stage-{repair}")
            for flag, enabled in (("pickup", picked_up), ("payment", paid)):
                if enabled:
                    revision = local.show_repair(ctx, repair).revision
                    local.change_repair_flag(ctx, repair, flag, {"revision": revision, "enabled": True},
                                             f"{flag}-{repair}")
            return repair

        made = []

        def make(stage, **flags):
            repair = local.create_repair(ctx, DEVICE, f"make-{len(made)}").id
            made.append(repair)
            if stage == "repaired":
                local.change_stage(ctx, repair, {"revision": 1, "stage": "in_progress"}, f"start-{repair}")
            return finish(repair, stage, **flags)

        waiting = make("repaired", picked_up=False, paid=False)
        collected = make("no_repair", picked_up=True, paid=False)
        make("repaired", picked_up=True, paid=True)  # paid: never listed
        make("no_repair", picked_up=False, paid=True)
        local.create_repair(ctx, DEVICE, "open-one")

        recipe = [line for line in SKILL.read_text().splitlines() if line.startswith("| all finished and unpaid")]
        commands = re.findall(r"`repairlib (repairs list [^`]+)`", recipe[0])
        self.assertEqual(len(commands), 2, recipe)

        def listed(args):
            done = subprocess.run([*COMMAND, *args.split(), "--workspace", str(fixture.workspace)], text=True,
                                  capture_output=True, timeout=30, env=python_env())
            self.assertEqual(done.returncode, 0, done.stderr)
            return {int(line.split("\t")[0]) for line in done.stdout.splitlines() if line.split("\t")[0].isdigit()}

        self.assertEqual(set().union(*(listed(command) for command in commands)), {waiting, collected})
        # The pickup queue alone is not the answer: it omits the collected repair.
        self.assertEqual(listed("repairs list --queue pickup --no-paid"), {waiting})
