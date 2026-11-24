"""Portable format 2 carries payment, fee links, device photo, warranty and shop contact (audit R04).

Format 1 stays readable: see tests/fixtures/portable/. Both fixtures are for future readers, so a
change that stops reading either one is a compatibility break, not a test to update.
"""

from contextlib import closing
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile, ZipInfo

from repairlib.client import local
from repairlib.domain import attachments
from repairlib.domain.setup import initialize_workspace
from test_repairs import DEVICE
from test_tokens import PASSWORD, TokenFixture

FIXTURES = Path(__file__).parent / "fixtures" / "portable"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)
CONTACT = {"language": "el", "address": "1 Synthetic Street", "phone": "210 000 0000",
           "terms": "Synthetic terms apply."}


def pack(files):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            entry = ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, payload)
    return buffer.getvalue()


def unpack(content):
    with ZipFile(BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def with_records(files, change):
    """The same archive with its records edited and the manifest hashes made consistent."""
    files = dict(files)
    manifest, records = json.loads(files["manifest.json"]), json.loads(files["records.json"])
    change(records)
    files["records.json"] = json.dumps(records).encode()
    manifest["files"]["records.json"] = {"size": len(files["records.json"]),
                                         "sha256": sha256(files["records.json"]).hexdigest()}
    files["manifest.json"] = json.dumps(manifest).encode()
    return files


class FormatTwoTests(TokenFixture, unittest.TestCase):
    def source_ticket(self):
        ctx = local.open_workspace(self.workspace)
        settings, _ = local.show_settings_page(ctx)
        local.edit_settings_page(ctx, {"revision": settings.revision, **CONTACT}, "contact")
        original = local.create_repair(ctx, DEVICE, "original").id
        returned = local.create_repair(ctx, DEVICE | {"warranty_until": "2031-02-03",
                                                     "warranty_repair_id": original}, "return").id
        local.set_fees(ctx, returned, {"diagnosis": 2000, "labor": 3000}, 1, "fees")
        local.change_stage(ctx, returned, {"revision": 2, "stage": "no_repair"}, "finish")
        local.change_repair_flag(ctx, returned, "payment", {"revision": 3, "enabled": True}, "paid")
        local.upload_attachment(ctx, returned, PNG, filename="fixture.png", entry_id=None, key="photo",
                                role="device")
        return ctx, original, returned

    def destination(self, stack):
        temp = stack.enter_context(TemporaryDirectory())
        path = Path(temp) / "workspace"
        initialize_workspace(path, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
        # Another shop's rows push destination IDs away from the source IDs, so remapping is observable.
        with closing(sqlite3.connect(path / "state" / "repairlib.sqlite3")) as db, db:
            db.execute("INSERT INTO shops(id, name) VALUES (2, 'Other')")
            db.execute("INSERT INTO users(id, shop_id, username, role, password_hash) "
                       "VALUES (99, 2, 'other', 'admin', '')")
            for number in range(1, 6):
                db.execute("INSERT INTO repairs(id, shop_id, reported_issue) VALUES (?, 2, 'Other')", (number,))
            for number in range(1, 8):
                db.execute("INSERT INTO repair_charges(id, shop_id, repair_id, kind, description, quantity_milli,"
                           "unit, unit_price_cents, created_by, created_by_username) "
                           "VALUES (?, 2, 1, 'labor', 'Other', 1000, 'h', 100, 99, 'other')", (number,))
        return path

    def export(self, ctx):
        buffer = BytesIO()
        size = local.download_portable_shop(ctx, buffer)
        buffer.seek(0)
        return buffer, size

    def rows(self, path, sql, *args):
        with closing(sqlite3.connect(path / "state" / "repairlib.sqlite3")) as db:
            return db.execute(sql, args).fetchall()

    def test_paid_ticket_round_trips_with_remapped_links_and_saving_fees_again_adds_nothing(self):
        from contextlib import ExitStack
        ctx, original, returned = self.source_ticket()
        archive, size = self.export(ctx)
        with ExitStack() as stack:
            path = self.destination(stack)
            dest = local.open_workspace(path)
            result = local.import_portable_shop(dest, archive, size, destination_shop_id=1, revision=1,
                                                key="import")
            self.assertEqual(result.omitted, ())
            (new_original, new_returned), = self.rows(
                path, "SELECT (SELECT id FROM repairs WHERE shop_id = 1 AND warranty_repair_id IS NULL), id "
                      "FROM repairs WHERE shop_id = 1 AND warranty_repair_id IS NOT NULL")
            self.assertNotIn(new_returned, (original, returned))  # the links had to be remapped
            repair = local.show_repair(dest, new_returned)
            self.assertEqual((repair.payment_paid, repair.warranty_until, repair.warranty_repair_id),
                             (True, "2031-02-03", new_original))
            fees = local.ticket_fees(dest, new_returned)
            self.assertEqual((fees["diagnosis"], fees["labor"], fees["total_cents"]), (2000, 3000, 5000))
            photo, _ = attachments.ticket_pictures(dest.workspace, dest.access, new_returned)
            self.assertIsNotNone(photo)
            settings, _ = local.show_settings_page(dest)
            self.assertEqual((settings.language, settings.address, settings.phone, settings.terms),
                             tuple(CONTACT[name] for name in ("language", "address", "phone", "terms")))
            local.set_fees(dest, new_returned, {"diagnosis": 2000, "labor": 3000}, repair.revision, "again")
            self.assertEqual(local.charge_summary(dest, new_returned).total_cents, 5000)
            self.assertEqual(self.rows(path, "SELECT count(*) FROM repair_charges WHERE repair_id = ?",
                                       new_returned), [(2,)])

    def test_invalid_references_are_refused_without_partial_writes(self):
        from contextlib import ExitStack
        ctx, _, returned = self.source_ticket()
        archive, _ = self.export(ctx)
        files = unpack(archive.getvalue())
        mistakes = {
            "fee link to a missing charge": lambda r: r["repair_fees"][0].update(labor_charge_id=9999),
            "fee link to another repair's charge": lambda r: (
                r["repair_charges"].append(r["repair_charges"][0] | {"id": 50, "repair_id": 1}),
                r["repair_fees"][0].update(labor_charge_id=50)),
            "warranty return of an unknown repair": lambda r: next(
                row for row in r["repairs"] if row["id"] == returned).update(warranty_repair_id=777),
            "language the app does not have": lambda r: r["shop_settings"][0].update(language="xx"),
        }
        with ExitStack() as stack:
            path = self.destination(stack)
            dest = local.open_workspace(path)
            before = self.rows(path, "SELECT (SELECT count(*) FROM repairs WHERE shop_id = 1), "
                                     "(SELECT count(*) FROM repair_charges WHERE shop_id = 1), "
                                     "(SELECT revision FROM shop_settings WHERE shop_id = 1), "
                                     "(SELECT count(*) FROM portable_imports)")
            for name, change in mistakes.items():
                bad = pack(with_records(files, change))
                with self.subTest(name), self.assertRaises(local.ClientError) as caught:
                    local.import_portable_shop(dest, BytesIO(bad), len(bad), destination_shop_id=1, revision=1,
                                               key="bad")
                self.assertEqual(caught.exception.code, "invalid_archive")
                self.assertEqual(self.rows(path, "SELECT (SELECT count(*) FROM repairs WHERE shop_id = 1), "
                                                 "(SELECT count(*) FROM repair_charges WHERE shop_id = 1), "
                                                 "(SELECT revision FROM shop_settings WHERE shop_id = 1), "
                                                 "(SELECT count(*) FROM portable_imports)"), before)

    def test_format_1_fixture_still_imports_with_its_disclosed_omissions(self):
        from contextlib import ExitStack
        content = (FIXTURES / "format1-paid-ticket.zip").read_bytes()
        with ExitStack() as stack:
            path = self.destination(stack)
            dest = local.open_workspace(path)
            result = local.import_portable_shop(dest, BytesIO(content), len(content), destination_shop_id=1,
                                                revision=1, key="old")
            self.assertTrue(result.omitted)
            preview = local.preview_portable_shop(dest, BytesIO(content), len(content))
            self.assertEqual((preview.format, preview.schema), ("repairlib-portable-shop-1", 21))
            (repair_id,), = self.rows(path, "SELECT id FROM repairs WHERE shop_id = 1")
            self.assertEqual(local.charge_summary(dest, repair_id).total_cents, 5000)
            self.assertIsNone(local.show_repair(dest, repair_id).payment_paid)

    def test_format_2_fixture_is_read_by_the_current_reader(self):
        content = (FIXTURES / "format2-paid-ticket.zip").read_bytes()
        ctx = local.open_workspace(self.workspace)
        preview = local.preview_portable_shop(ctx, BytesIO(content), len(content))
        self.assertEqual((preview.format, preview.schema, preview.omitted), ("repairlib-portable-shop-2", 43, ()))
        self.assertEqual((preview.counts["repair_fees"], preview.counts["repairs"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
