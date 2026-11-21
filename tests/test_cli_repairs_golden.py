"""Every `repairs` action's CLI output (plain and JSON) and exit code, frozen in golden/cli_repairs.txt.

The scenario runs on a disposable workspace with fixed keys; timestamps and the temp path are normalized.
Restructuring the command dispatcher must leave the transcript byte-identical. Regenerate on purpose with
`UPDATE_GOLDEN=1` and review the diff like any CLI contract change.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from repairlib.__main__ import main
from support.workspace import clone_workspace
from test_tokens import PASSWORD

GOLDEN = Path(__file__).with_name("golden") / "cli_repairs.txt"
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")


class Scenario:
    def __init__(self, workspace: Path, scratch: Path):
        self.workspace, self.scratch, self.lines, self.keys = workspace, scratch, [], 0

    def run(self, *args, json_out=False):
        """Run one CLI command in-process; record it; return parsed JSON when asked and valid."""
        self.keys += 1
        argv = [*args, "--workspace", str(self.workspace)]
        if "--key" in args:
            argv[argv.index("--key") + 1] = f"golden-{self.keys}"
        if json_out:
            argv.append("--json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        shown = " ".join(a for a in args if not str(a).startswith(str(self.scratch)))
        text = (out.getvalue() + ("[stderr] " + err.getvalue() if err.getvalue() else ""))
        text = STAMP.sub("<ts>", text.replace(str(self.scratch), "<tmp>").replace(str(self.workspace), "<ws>"))
        self.lines.append(f"$ {shown}{' --json' if json_out else ''}  -> exit {code}\n{text.rstrip()}\n")
        if json_out:
            try:
                return json.loads(out.getvalue())
            except ValueError:
                return {}
        return None

    def quiet(self, *args):
        """Run without recording (revision lookups)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main([*args, "--workspace", str(self.workspace), "--json"])
        return json.loads(out.getvalue())


def transcript() -> str:
    with TemporaryDirectory() as temp:
        scratch = Path(temp)
        workspace = scratch / "workspace"
        clone_workspace(workspace, PASSWORD)
        s = Scenario(workspace, scratch)
        R = lambda: s.quiet("repairs", "show", "1")["revision"]
        s.run("customers", "create", "--key", "k", "--name", "Golden Customer")
        s.run("inventory", "create", "--key", "k", "--sku", "GOLD-1", "--description", "Golden part",
              "--purchase-cost", "1.00", "--selling-price", "2.50", "--low-stock", "1")
        s.run("inventory", "receive", "1", "--quantity", "10", "--reason", "stock", "--revision", "1", "--key", "k")
        s.run("repairs", "create", "--key", "k", "--customer", "1", "--device-type", "Laptop", "--brand", "Acme",
              "--model", "X1", "--serial", "SN-GOLD", "--reported-issue", "No power")
        s.run("repairs", "create", "--key", "k", "--device-type", "Phone", "--reported-issue", "Cracked screen",
              json_out=True)
        s.run("repairs", "show", "1"); s.run("repairs", "show", "1", json_out=True)
        s.run("repairs", "list"); s.run("repairs", "list", "--queue", "open", json_out=True)
        s.run("repairs", "device-history", "--serial", "SN-GOLD")
        s.run("repairs", "edit", "1", "--revision", str(R()), "--model", "X2", "--key", "k")
        s.run("repairs", "stage", "1", "--revision", str(R()), "--stage", "in_progress", "--key", "k")
        # work entries
        s.run("repairs", "add-entry", "1", "--kind", "work", "--body", "Opened the case", "--key", "k")
        s.run("repairs", "entries", "1"); s.run("repairs", "entries", "1", json_out=True)
        s.run("repairs", "edit-entry", "1", "1", "--revision", "1", "--kind", "note", "--body", "Opened case",
              "--reason", "typo", "--key", "k")
        s.run("repairs", "delete-entry", "1", "1", "--revision", "2", "--reason", "mistake", "--key", "k")
        s.run("repairs", "entries", "1")
        s.run("repairs", "restore-entry", "1", "1", "--revision", "3", "--reason", "back", "--key", "k")
        s.run("repairs", "entry-history", "1", "1")
        s.run("repairs", "activity", "1"); s.run("repairs", "activity", "1", "--limit", "2", json_out=True)
        # charges and discount
        s.run("repairs", "add-charge", "1", "--kind", "part", "--description", "Fan", "--quantity", "1",
              "--unit-price", "12.50", "--unit-cost", "5", "--key", "k")
        s.run("repairs", "add-charge", "1", "--kind", "labor", "--description", "Work", "--quantity", "0.5",
              "--unit", "h", "--unit-price", "40", "--key", "k", json_out=True)
        s.run("repairs", "charges", "1"); s.run("repairs", "charges", "1", json_out=True)
        s.run("repairs", "edit-charge", "1", "1", "--revision", "1", "--unit-price", "13", "--reason", "price", "--key", "k")
        s.run("repairs", "delete-charge", "1", "2", "--revision", "1", "--reason", "remove", "--key", "k")
        s.run("repairs", "restore-charge", "1", "2", "--revision", "2", "--reason", "keep", "--key", "k")
        s.run("repairs", "discount", "1", "--revision", "0", "--kind", "percent", "--value", "10", "--key", "k")
        s.run("repairs", "discount", "1", "--revision", "1", "--kind", "none", "--key", "k", json_out=True)
        # stock-linked parts
        s.run("repairs", "consume-part", "1", "1", "--repair-revision", str(R()), "--item-revision", "2",
              "--consumed-quantity", "2", "--billed-quantity", "2", "--key", "k")
        s.run("repairs", "charges", "1", "--include-consumption")
        s.run("repairs", "part-return-status", "1", "3")
        s.run("repairs", "return-part", "1", "3", "--repair-revision", str(R()), "--item-revision", "3",
              "--quantity", "0.5", "--reason", "unused", "--key", "k")
        s.run("repairs", "consume-part", "1", "1", "--repair-revision", str(R()), "--item-revision", "4",
              "--consumed-quantity", "1", "--billed-quantity", "1", "--key", "k", json_out=True)
        s.run("repairs", "intake-mistake", "1", "4", "--repair-revision", str(R()), "--item-revision", "5",
              "--charge-revision", "1", "--reason", "wrong", "--key", "k")
        s.run("repairs", "consume-part", "1", "1", "--repair-revision", str(R()), "--item-revision", "999",
              "--consumed-quantity", "1000", "--billed-quantity", "1", "--key", "k")
        s.run("repairs", "add-charge", "1", "--kind", "part", "--description", "Bad", "--quantity", "x",
              "--unit-price", "1", "--key", "k")
        # attachments
        source = scratch / "photo.bin"
        source.write_bytes(b"golden original bytes")
        s.run("repairs", "attach", "1", str(source), "--entry", "1", "--key", "k")
        s.run("repairs", "attachments", "1"); s.run("repairs", "attachments", "1", json_out=True)
        s.run("repairs", "edit-attachment", "1", "1", "--revision", "1", "--caption", "Board", "--key", "k")
        s.run("repairs", "unlink-attachment", "1", "1", "1", "--revision", "2", "--key", "k")
        s.run("repairs", "link-attachment", "1", "1", "1", "--revision", "3", "--key", "k")
        s.run("repairs", "download", "1", "1", "--output", str(scratch / "copy.bin"))
        s.run("repairs", "download", "1", "1", "--output", str(scratch / "copy.bin"))
        s.run("repairs", "detach", "1", "1", "--revision", "4", "--key", "k")
        s.run("repairs", "purge-attachment", "1", "1", "--revision", "5", "--key", "k")
        s.run("repairs", "detach", "1", "1", "--revision", "5", "--undo", "--key", "k")
        # lifecycle flags, labels and refusals
        s.run("repairs", "stage", "1", "--revision", str(R()), "--stage", "repaired", "--key", "k")
        s.run("repairs", "stage", "1", "--revision", str(R()), "--stage", "in_progress", "--key", "k")
        s.run("repairs", "stage", "1", "--revision", str(R()), "--stage", "repaired", "--key", "k")
        s.run("repairs", "pickup", "1", "--revision", str(R()), "--key", "k")
        s.run("repairs", "pickup", "1", "--revision", str(R()), "--undo", "--key", "k")
        s.run("repairs", "archive", "1", "--revision", str(R()), "--key", "k")
        s.run("repairs", "archive", "1", "--revision", str(R()), "--undo", "--key", "k")
        s.run("repairs", "label-options", "1"); s.run("repairs", "label-options", "1", json_out=True)
        s.run("repairs", "print-label", "1", "--revision", str(R()), "--printer-revision", "1", "--key", "k")
        s.run("repairs", "show", "99")
        s.run("repairs", "edit", "1", "--revision", "1", "--model", "stale", "--key", "k")
        return "\n".join(s.lines)


class CliRepairsGoldenTests(unittest.TestCase):
    def test_transcript_matches_the_golden_file(self):
        with patch("repairlib.domain.printing.p15_binary", return_value="/synthetic/p15"):
            text = transcript()
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN.write_text(text, encoding="utf8")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf8"),
                         "CLI output changed. If intended, rerun with UPDATE_GOLDEN=1 and review the diff.")


if __name__ == "__main__":
    unittest.main()
