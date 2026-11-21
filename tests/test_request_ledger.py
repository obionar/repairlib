"""One replay helper with a static request-table allowlist."""

import ast
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from repairlib.contracts import (ChargeInput, CustomerInput, ItemInput, RepairInput,
                                 SettingsPatch, UserInput)
from repairlib.domain.customers import CustomerAccess
from repairlib.lifecycle.workspace import database_path
from repairlib.storage import initialization

SRC = Path(__file__).resolve().parents[1] / "src" / "repairlib"
FAMILIES = ("attachment", "charge", "csv", "customer", "inventory", "printer",
            "repair", "settings", "stock", "user")
# Request-table SQL that is deliberately not a replay lookup/insert and stays static text.
STATIC_USES = {"storage/customers.py", "storage/printer.py", "storage/stock_reconciliation.py",
               "storage/portable_shop.py"}


# stock_reconciliation interpolates only these fixed, code-defined SQL fragments next to a static
# `FROM stock_requests`; neither is a table name or comes from a caller.
FIXED_FRAGMENTS = {"imported", "import_join"}


@dataclass(frozen=True)
class Result:
    id: int
    value: str


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        self.workspace.mkdir()
        self.path = database_path(self.workspace)
        self.path.parent.mkdir()
        initialization.initialize_database(self.path, "Shop", "UTC", "owner", "unused")
        from repairlib.domain.users import ensure_local_actor
        ensure_local_actor(self.workspace, "cli")
        self.access = CustomerAccess("cli")

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, args).fetchall()

    def counts(self, table):
        return (self.rows(f"SELECT COUNT(*) FROM {table}")[0][0],
                self.rows("SELECT COUNT(*) FROM audit_events")[0][0])

    def replays(self, table, first, second, different, refusal=(409, "conflict")):
        """Same key twice replays; no second row or audit event; a different payload is refused."""
        before = self.counts(table)
        created = first()
        after = self.counts(table)
        self.assertEqual(after[0], before[0] + 1, table)
        self.assertGreater(after[1], before[1], table)
        stored = self.rows(f"SELECT * FROM {table}")
        self.assertEqual(second(), created)
        self.assertEqual(self.counts(table), after)
        self.assertEqual(self.rows(f"SELECT * FROM {table}"), stored)
        with self.assertRaises(Exception) as captured:
            different()
        self.assertEqual((captured.exception.status, captured.exception.code), refusal)
        self.assertEqual(self.counts(table), after)
        self.assertEqual(self.rows(f"SELECT * FROM {table}"), stored)

    def test_customer_replays(self):
        from repairlib.domain import customers
        a, b = CustomerInput(name="Ada"), CustomerInput(name="Bea")
        self.replays("customer_requests",
                     lambda: customers.create_customer(self.workspace, self.access, a, key="k1"),
                     lambda: customers.create_customer(self.workspace, self.access, a, key="k1"),
                     lambda: customers.create_customer(self.workspace, self.access, b, key="k1"))

    def test_user_replays(self):
        from repairlib.domain import users
        a = UserInput(username="tech", role="technician", password="tech passphrase")
        b = UserInput(username="other", role="technician", password="tech passphrase")
        self.replays("user_requests",
                     lambda: users.create_user(self.workspace, self.access, a, key="k1"),
                     lambda: users.create_user(self.workspace, self.access, a, key="k1"),
                     lambda: users.create_user(self.workspace, self.access, b, key="k1"))

    def test_inventory_and_stock_replay(self):
        from repairlib.domain import inventory, stock
        a, b = ItemInput(sku="A", description="A", unit="pcs"), ItemInput(sku="B", description="B", unit="pcs")
        item = []
        self.replays("inventory_requests",
                     lambda: item.append(inventory.create_item(self.workspace, self.access, a, key="k1")) or item[0],
                     lambda: inventory.create_item(self.workspace, self.access, a, key="k1"),
                     lambda: inventory.create_item(self.workspace, self.access, b, key="k1"))
        self.replays("stock_requests",
                     lambda: stock.receive_stock(self.workspace, self.access, item[0].id, 5000,
                                                 revision=item[0].revision, key="k2"),
                     lambda: stock.receive_stock(self.workspace, self.access, item[0].id, 5000,
                                                 revision=item[0].revision, key="k2"),
                     lambda: stock.receive_stock(self.workspace, self.access, item[0].id, 6000,
                                                 revision=item[0].revision, key="k2"))

    def test_settings_replays(self):
        from repairlib.domain import settings
        self.replays("settings_requests",
                     lambda: settings.update_settings(self.workspace, self.access,
                                                      SettingsPatch(revision=1, name="Renamed"), key="k1"),
                     lambda: settings.update_settings(self.workspace, self.access,
                                                      SettingsPatch(revision=1, name="Renamed"), key="k1"),
                     lambda: settings.update_settings(self.workspace, self.access,
                                                      SettingsPatch(revision=1, name="Another"), key="k1"))

    def test_repair_and_charge_replay(self):
        from repairlib.domain import charges, repairs
        a, b = RepairInput(device_type="Phone", reported_issue="Dead"), RepairInput(device_type="Radio", reported_issue="Dead")
        repair = []
        self.replays("repair_requests",
                     lambda: repair.append(repairs.create_repair(self.workspace, self.access, a, key="k1")) or repair[0],
                     lambda: repairs.create_repair(self.workspace, self.access, a, key="k1"),
                     lambda: repairs.create_repair(self.workspace, self.access, b, key="k1"))
        part = ChargeInput(kind="labor", description="Bench time", quantity_milli=1000, unit_price_cents=2500)
        other = ChargeInput(kind="labor", description="Other", quantity_milli=1000, unit_price_cents=2500)
        rid = repair[0].id
        self.replays("charge_requests",
                     lambda: charges.create_charge(self.workspace, self.access, rid, part, key="k2"),
                     lambda: charges.create_charge(self.workspace, self.access, rid, part, key="k2"),
                     lambda: charges.create_charge(self.workspace, self.access, rid, other, key="k2"))

    def valid_scope(self, spec):
        """First value the table's own CHECK constraint accepts for its scope column."""
        if spec.table == "printer_requests":  # Its CHECK ties repair_id to the label operation.
            return "printer.label"
        sql = self.rows("SELECT sql FROM sqlite_master WHERE name = ?", spec.table)[0][0]
        found = re.search(rf"{spec.scope} IN\s*\(\s*'([^']+)'", sql)
        return found[1] if found else "x.op"  # Unconstrained scope column.

    def test_helper_serves_every_family_and_isolates_scope(self):
        from repairlib.storage import request_ledger as ledger
        self.assertEqual(set(ledger.FAMILIES), set(FAMILIES))
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            for family in FAMILIES:
                spec = ledger.FAMILIES[family]
                operation = self.valid_scope(spec) if spec.scope else None
                self.assertIsNone(ledger.find(db, family, 1, 1, operation, "key-1"))
                link = 7 if spec.link else None
                ledger.store(db, family, 1, 1, operation, "key-1", "fp", Result(7, "v"), link=link)
                found = ledger.find(db, family, 1, 1, operation, "key-1")
                self.assertEqual((found["fingerprint"], found["result"]), ("fp", '{"id": 7, "value": "v"}'))
                self.assertIsNone(ledger.find(db, family, 2, 1, operation, "key-1"), family)
                self.assertIsNone(ledger.find(db, family, 1, 2, operation, "key-1"), family)
                self.assertIsNone(ledger.find(db, family, 1, 1, operation, "key-2"), family)
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.store(db, family, 1, 1, operation, "key-1", "fp", Result(7, "v"), link=link)

    def test_allowlist_refuses_unknown_names(self):
        from repairlib.storage import request_ledger as ledger
        with closing(sqlite3.connect(self.path)) as db:
            for bad in ("nonsense", "users", "audit_events; DROP TABLE users", "customer_requests", ""):
                with self.assertRaises(ledger.UnknownFamily):
                    ledger.find(db, bad, 1, 1, "x", "k")
                with self.assertRaises(ledger.UnknownFamily):
                    ledger.store(db, bad, 1, 1, "x", "k", "fp", Result(1, "v"))
            with self.assertRaises(ledger.UnknownFamily):
                ledger.find(db, ("customer_requests",), 1, 1, "x", "k")
        self.assertEqual(self.counts("user_requests")[0], 0)

    def test_every_allowlisted_table_exists_and_is_scanned(self):
        from repairlib.storage import request_ledger as ledger
        with closing(sqlite3.connect(self.path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for spec in ledger.FAMILIES.values():
            self.assertIn(spec.table, tables)
        found = {name for name in tables if name.endswith("_requests")}
        self.assertEqual(found, {spec.table for spec in ledger.FAMILIES.values()} | {"stock_requests"})

    def test_no_request_table_sql_is_assembled_from_a_variable(self):
        """Outside request_ledger, request tables appear only as static literals; inside it, only from the mapping."""
        dynamic = []
        for source in sorted(SRC.rglob("*.py")):
            relative = source.relative_to(SRC).as_posix()
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant))
                    names = {n.id for p in node.values if isinstance(p, ast.FormattedValue)
                             for n in ast.walk(p.value) if isinstance(n, ast.Name)}
                    if relative == "storage/request_ledger.py":
                        if names - {"spec", "table", "scope", "columns", "marks"}:
                            dynamic.append((relative, node.lineno, "unmapped name"))
                    elif re.search(r"_requests\b", literal) and names - FIXED_FRAGMENTS:
                        dynamic.append((relative, node.lineno, "f-string"))
                elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
                    parts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                    if any(re.search(r"_requests\b", p) for p in parts):
                        dynamic.append((relative, node.lineno, "concatenation"))
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"
                      and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)
                      and re.search(r"_requests\b", node.func.value.value)):
                    dynamic.append((relative, node.lineno, "format"))
        self.assertEqual(dynamic, [])
        literal_uses = set()
        for source in sorted(SRC.rglob("*.py")):
            relative = source.relative_to(SRC).as_posix()
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and re.search(r"\b(FROM|INTO|UPDATE)\s+\w+_requests\b", node.value)):
                    literal_uses.add(relative)
        self.assertEqual(literal_uses, STATIC_USES & literal_uses)
        self.assertEqual(literal_uses, STATIC_USES - {"storage/portable_shop.py"})

    def test_switched_storage_modules_no_longer_hold_replay_sql(self):
        for name in ("attachments", "charges", "csv_intake", "inventory", "repairs",
                     "settings", "stock", "users"):
            text = (SRC / "storage" / f"{name}.py").read_text()
            self.assertIsNone(re.search(r"\w+_requests\b", text), name)
        self.assertIsNone(re.search(r"(SELECT|INSERT INTO)[^\"]*customer_requests",
                                    (SRC / "storage" / "customers.py").read_text()))


if __name__ == "__main__":
    unittest.main()
