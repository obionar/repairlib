"""Text the app writes onto a ticket uses the shop's language (D6); English stays the default."""

from contextlib import closing
import sqlite3
import unittest

from repairlib.contracts import ChargeInput
from repairlib.domain import charges, repairs
import test_repair_consumption as fixture


class ShopLanguageTextTests(unittest.TestCase):
    token = fixture.ConsumptionTests.token
    use = fixture.ConsumptionTests.use

    def setUp(self):
        fixture.ConsumptionTests.setUp(self)

    def speak(self, language):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE shop_settings SET language = ?", (language,))

    def texts(self, repair):
        entries = [entry.body for entry in repairs.list_work_entries(self.workspace, self.tech, repair.id)[0].items]
        items = charges.charge_summary(self.workspace, self.tech, repair.id)[0].items
        return entries, [(item.description, item.unit) for item in items]

    def write_all(self, repair, suffix):
        self.use(self.tech, repair, key=f"use-{suffix}")
        current = repairs.read_repair(self.workspace, self.tech, repair.id)[0].revision
        charges.create_charge(self.workspace, self.tech, repair.id, ChargeInput(
            kind="part", description="Fuse", quantity_milli=1000, unit_price_cents=250),
            key=f"manual-{suffix}", part_revision=current)
        current = repairs.read_repair(self.workspace, self.tech, repair.id)[0].revision
        charges.set_fees(self.workspace, self.tech, repair.id, diagnosis=1000, labor=2000,
                         discount=None, revision=current, key=f"fees-{suffix}")

    def test_greek_shop_writes_greek_ticket_text(self):
        self.speak("el")
        self.write_all(self.first, "el")
        entries, items = self.texts(self.first)
        self.assertIn("Ανταλλακτικό: Fuse × 4 τεμ.", entries)
        self.assertIn("Ανταλλακτικό: Fuse × 1 τεμ.", entries)
        self.assertIn(("Fuse", "τεμ."), items)
        self.assertIn(("Διάγνωση", "ώρ."), items)
        self.assertIn(("Εργασία", "ώρ."), items)

    def test_unset_language_keeps_english(self):
        self.write_all(self.first, "en")
        entries, items = self.texts(self.first)
        self.assertIn("Part used: Fuse × 1 pcs", entries)
        self.assertIn(("Labour", "h"), items)


if __name__ == "__main__":
    unittest.main()
