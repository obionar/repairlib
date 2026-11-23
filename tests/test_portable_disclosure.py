"""Format 1 says what it cannot carry, and the statement matches a real round trip (audit R04).

When a successor format carries these items, update the disclosure and this test together.
"""

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from repairlib.client import local
from repairlib.contracts import PortablePreview
from repairlib.domain import attachments
from repairlib.domain.setup import initialize_workspace
from test_baseline import python_env
from test_cli_identity import COMMAND
from test_repairs import DEVICE
from test_tokens import PASSWORD, TokenFixture


class PortableDisclosureTests(TokenFixture, unittest.TestCase):
    def finished_paid_ticket(self, ctx):
        repair = local.create_repair(ctx, DEVICE, "repair").id
        local.set_fees(ctx, repair, {"diagnosis": 2000, "labor": 3000}, 1, "fees")
        local.change_stage(ctx, repair, {"revision": 2, "stage": "no_repair"}, "finish")
        local.change_repair_flag(ctx, repair, "payment", {"revision": 3, "enabled": True}, "paid")
        local.upload_attachment(ctx, repair, b"\x89PNG\r\n\x1a\n" + bytes(16), filename="fixture.png",
                                entry_id=None, key="photo", role="device")
        return repair

    def state(self, ctx, repair):
        photo, _ = attachments.ticket_pictures(ctx.workspace, ctx.access, repair)
        fees = local.ticket_fees(ctx, repair)
        return (local.show_repair(ctx, repair).payment_paid, fees["diagnosis"], fees["labor"],
                local.charge_summary(ctx, repair).total_cents, photo is not None)

    def test_disclosed_omissions_are_real_and_the_charge_lines_survive(self):
        source = local.open_workspace(self.workspace)
        repair = self.finished_paid_ticket(source)
        self.assertEqual(self.state(source, repair), (True, 2000, 3000, 5000, True))
        buffer = BytesIO()
        size = local.download_portable_shop(source, buffer)
        buffer.seek(0)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "workspace"
            initialize_workspace(path, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
            dest = local.open_workspace(path)
            local.import_portable_shop(dest, buffer, size, destination_shop_id=1, revision=1, key="import")
            paid, diagnosis, labor, total, photo = self.state(dest, repair)
            text = " ".join(PortablePreview.omitted)
            for claim, lost in (("payment status", not paid), ("diagnosis and repair fees", (diagnosis, labor) == (0, 0)),
                                ("device photo", not photo)):
                self.assertIn(claim, text)
                self.assertTrue(lost, f"{claim} is disclosed as omitted but survived; update the disclosure")
            self.assertEqual(total, 5000)  # The charge lines themselves are carried.

    def test_cli_discloses_on_export_and_import_and_json_preview_carries_the_list(self):
        source = local.open_workspace(self.workspace)
        self.finished_paid_ticket(source)
        with TemporaryDirectory() as temp:
            parent = Path(temp) / "out"
            parent.mkdir(mode=0o700)
            archive = parent / "shop.zip"
            run = lambda *args: subprocess.run([*COMMAND, "portable", *args, "--workspace", str(self.workspace)],
                                               text=True, capture_output=True, timeout=60, env=python_env())
            exported = run("export", str(archive))
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertIn("does not carry", exported.stderr)
            self.assertIn("payment status", exported.stderr)
            self.assertIn("full backup", exported.stderr)
            previewed = run("preview", str(archive), "--json")
            self.assertIn('"omitted"', previewed.stdout)
            self.assertNotIn("does not carry", previewed.stderr)
            self.assertIn("does not carry", run("preview", str(archive)).stderr)


if __name__ == "__main__":
    unittest.main()
