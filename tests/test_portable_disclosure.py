"""Format 1 still says what it cannot carry, and the statement matches a real import (audit R04).

Format 2 carries these items (test_portable_format2); a format-1 archive still lacks them, so the
disclosure stays for as long as format 1 is readable.
"""

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from repairlib.client import local
from repairlib.contracts import FORMAT_1_OMITTED
from repairlib.domain import attachments
from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_tokens import PASSWORD, TokenFixture

FIXTURE = Path(__file__).parent / "fixtures" / "portable" / "format1-paid-ticket.zip"


class PortableDisclosureTests(TokenFixture, unittest.TestCase):
    def test_format_1_disclosed_omissions_are_real_and_the_charge_lines_survive(self):
        content = FIXTURE.read_bytes()
        with TemporaryDirectory() as temp:
            path = Path(temp) / "workspace"
            initialize_workspace(path, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
            dest = local.open_workspace(path)
            preview = local.preview_portable_shop(dest, BytesIO(content), len(content))
            self.assertEqual(preview.omitted, FORMAT_1_OMITTED)
            local.import_portable_shop(dest, BytesIO(content), len(content), destination_shop_id=1, revision=1,
                                       key="import")
            repair = 1
            fees = local.ticket_fees(dest, repair)
            photo, _ = attachments.ticket_pictures(dest.workspace, dest.access, repair)
            text = " ".join(preview.omitted)
            for claim, lost in (("payment status", local.show_repair(dest, repair).payment_paid is None),
                                ("diagnosis and repair fees", (fees["diagnosis"], fees["labor"]) == (0, 0)),
                                ("device photo", photo is None)):
                self.assertIn(claim, text)
                self.assertTrue(lost, f"{claim} is disclosed as omitted but survived; update the disclosure")
            self.assertEqual(local.charge_summary(dest, repair).total_cents, 5000)

    def test_cli_discloses_format_1_archives_only(self):
        local.open_workspace(self.workspace)
        with TemporaryDirectory() as temp:
            parent = Path(temp) / "out"
            parent.mkdir(mode=0o700)
            archive = parent / "shop.zip"

            def run(*args):
                return subprocess.run([*COMMAND, "portable", *args, "--workspace", str(self.workspace)],
                                      text=True, capture_output=True, timeout=60, env=python_env())

            exported = run("export", str(archive))
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertNotIn("does not carry", exported.stderr)  # Format 2 omits nothing.
            old = run("preview", str(FIXTURE), "--json")
            self.assertIn("payment status", old.stdout)
            self.assertNotIn("does not carry", old.stderr)
            human = run("preview", str(FIXTURE))
            self.assertIn("does not carry", human.stderr)
            self.assertIn("full backup", human.stderr)
            self.assertNotIn("does not carry", run("preview", str(archive)).stderr)


if __name__ == "__main__":
    unittest.main()
