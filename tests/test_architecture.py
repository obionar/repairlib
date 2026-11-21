"""Architecture tests enforcing the AGENTS.md invariants with an S1a.1 owner."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "repairlib"

FORBIDDEN_DOMAINS = (
    "fastapi", "jinja2", "httpx", "requests", "aiohttp",
    "repairlib.adapters", "repairlib.app", "repairlib.__main__",
    "repairlib.cli", "repairlib.client", "repairlib.mcp",
)


def walk_modules(directory: Path):
    for path in sorted(directory.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def module_name(path: Path) -> str:
    """Dotted name of a module under the repairlib package."""
    return ".".join(("repairlib", *path.relative_to(PACKAGE).with_suffix("").parts))


# repairlib.i18n parses Accept-Language q-values with float() for ranking only.
FLOAT_ALLOWLIST = frozenset({module_name(PACKAGE / "i18n.py")})

# Money is integer cents and quantities are integer milli-units, so true division,
# augmented division and float literals must not appear where those values are
# computed. A package-wide ban is impossible without type information: the rest of
# the package uses `/` for path joins (`workspace / ACTIVE`) at ~50 sites, which
# an AST test cannot tell apart from arithmetic. `domain/` is pure integer logic
# and holds no path joins, so the whole tree is covered.
MONEY_MODULES = (
    PACKAGE / "money.py",
    PACKAGE / "storage" / "charges.py",
    PACKAGE / "storage" / "repairs.py",
    PACKAGE / "storage" / "stock.py",
)


def float_hits(tree: ast.Module, path: Path) -> list[str]:
    """float() calls, including builtins.float and aliased imports, outside domain."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if name == "float":
            hits.append(f"{path}: line {node.lineno}: float()")
    return hits


def true_division_hits(tree: ast.Module, path: Path) -> list[str]:
    """True division in any spelling: `a / b` and `a /= b` (an AugAssign node)."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            hits.append(f"{path}: line {node.lineno}: true division {ast.unparse(node)}")
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
            hits.append(f"{path}: line {node.lineno}: true division {ast.unparse(node)}")
    return hits


def float_literal_hits(tree: ast.Module, path: Path) -> list[str]:
    """Float literals, which turn integer cents into floats under any operator."""
    return [f"{path}: line {node.lineno}: float literal {node.value!r}" for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)]


class ArchitectureTests(unittest.TestCase):
    def test_sqlite_only_in_storage(self) -> None:
        storage = PACKAGE / "storage"
        self.assertTrue(storage.is_dir(), "storage package must exist to own sqlite3")
        violations: list[str] = []
        for path, tree in walk_modules(PACKAGE):
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name == "sqlite3" or name.startswith("sqlite3."):
                        if not path.is_relative_to(storage):
                            violations.append(f"{path}: line {node.lineno}")
        self.assertEqual(violations, [], f"sqlite3 used outside storage: {violations}")

    def test_domain_has_no_adapter_imports(self) -> None:
        domain = PACKAGE / "domain"
        self.assertTrue(domain.is_dir(), "domain package must exist to host shared operations")
        violations: list[str] = []
        for path, tree in walk_modules(domain):
            package = ".".join(("repairlib", *path.parent.relative_to(PACKAGE).parts))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = resolve_name("." * node.level + (node.module or ""), package)
                    names = [f"{module}.{alias.name}" for alias in node.names]
                for name in names:
                    if any(name == forbidden or name.startswith(forbidden + ".")
                           for forbidden in FORBIDDEN_DOMAINS):
                        violations.append(f"{path}: line {node.lineno}: {name}")
        self.assertEqual(violations, [], f"adapter import in domain: {violations}")

    def test_applied_migration_files_unchanged(self) -> None:
        manifest = ROOT / "tests" / "migration-hashes.txt"
        hashes = {
            path: digest for digest, path in
            (line.split(maxsplit=1) for line in manifest.read_text().splitlines())
        }
        actual = {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (PACKAGE / "storage" / "migrations").glob("*.sql")
        }
        self.assertTrue(actual)
        self.assertEqual(actual, hashes)

    def test_http_adapters_and_client_do_not_import_storage(self) -> None:
        violations = []
        for directory in ("adapters", "cli", "client"):
            for path, tree in walk_modules(PACKAGE / directory):
                package = ".".join(("repairlib", *path.parent.relative_to(PACKAGE).parts))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        module = resolve_name("." * node.level + (node.module or ""), package)
                        names = [f"{module}.{alias.name}" for alias in node.names]
                    else:
                        continue
                    if any(name == "repairlib.storage" or name.startswith("repairlib.storage.")
                           for name in names):
                        violations.append(f"{path}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_shop_scoped_tables_have_non_null_shop_id_and_composite_fks(self) -> None:
        from repairlib.storage.initialization import initialize_database

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            initialize_database(path, "Synthetic Shop", "UTC", "owner", "synthetic-password-hash")
            with closing(sqlite3.connect(path)) as db:
                tables = [
                    row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                columns = {
                    table: {
                        row[1]: row for row in db.execute(f'PRAGMA table_info("{table}")')
                    }
                    for table in tables
                }
                scoped = {table for table, cols in columns.items() if "shop_id" in cols}
                self.assertTrue(scoped, "expected at least one shop-scoped table")
                violations: list[str] = []
                for table in sorted(scoped):
                    _, _, _, notnull, _, pk = columns[table]["shop_id"]
                    if not (notnull or pk):
                        violations.append(f"{table}.shop_id is nullable")
                    groups: dict[int, list[tuple]] = {}
                    for row in db.execute(f'PRAGMA foreign_key_list("{table}")'):
                        groups.setdefault(row[0], []).append(row)
                    for fk_id, rows in sorted(groups.items()):
                        rows.sort(key=lambda row: row[1])
                        parent = rows[0][2]
                        if parent not in scoped:
                            continue
                        child_columns = [row[3] for row in rows]
                        if child_columns[0] != "shop_id":
                            violations.append(
                                f"{table} FK {fk_id} -> {parent}{tuple(child_columns)} "
                                "does not start with shop_id"
                            )
        self.assertEqual(violations, [], f"shop isolation violations: {violations}")

    def test_domain_does_not_use_float(self) -> None:
        violations: list[str] = []
        for path, tree in walk_modules(PACKAGE / "domain"):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "float"):
                    violations.append(f"{path}: line {node.lineno}")
        self.assertEqual(violations, [], f"float() used in domain: {violations}")

    def test_no_float_outside_domain(self) -> None:
        hits: list[str] = []
        for path, tree in walk_modules(PACKAGE):
            if path.is_relative_to(PACKAGE / "domain") or module_name(path) in FLOAT_ALLOWLIST:
                continue
            hits.extend(float_hits(tree, path))
        self.assertEqual(hits, [], f"float() outside domain: {hits}")

    def money_sources(self):
        for path in MONEY_MODULES:
            self.assertTrue(path.is_file(), f"money module missing: {path}")
            yield path, ast.parse(path.read_text(encoding="utf-8"))
        yield from walk_modules(PACKAGE / "domain")

    def test_no_true_division_in_money_modules(self) -> None:
        hits = [hit for path, tree in self.money_sources() for hit in true_division_hits(tree, path)]
        self.assertEqual(hits, [], f"true division in money code: {hits}")

    def test_no_float_literals_in_money_code(self) -> None:
        hits = [hit for path, tree in self.money_sources() for hit in float_literal_hits(tree, path)]
        self.assertEqual(hits, [], f"float literal in money code: {hits}")

    def test_float_div_detector_flags_synthetic_float_and_division(self) -> None:
        # Both detectors are shared by the guards and this self-test, so a
        # regression that stops reporting a pattern breaks both tests.
        floats = float_hits(ast.parse("float(x)\n", filename="synthetic.py"), Path("synthetic.py"))
        self.assertTrue(any("float()" in hit for hit in floats),
                        f"detector missed the synthetic float(x): {floats}")
        # A bound divisor is the common money bug and must always be reported.
        divisions = true_division_hits(
            ast.parse("total = 1\ncount = 2\nprint(total / count)\n", filename="synthetic.py"),
            Path("synthetic.py"))
        self.assertTrue(any("total / count" in hit for hit in divisions),
                        f"detector missed a bound-name division: {divisions}")
        # Augmented division and float literals are separate spellings of the same bug.
        augmented = true_division_hits(ast.parse("total /= 100\n", filename="synthetic.py"),
                                       Path("synthetic.py"))
        self.assertTrue(any("total /= 100" in hit for hit in augmented),
                        f"detector missed an augmented division: {augmented}")
        literals = float_literal_hits(ast.parse("rate = 1.5\n", filename="synthetic.py"),
                                      Path("synthetic.py"))
        self.assertTrue(any("1.5" in hit for hit in literals),
                        f"detector missed a float literal: {literals}")
        # builtins.float must not slip past the bare-name check.
        qualified = float_hits(ast.parse("builtins.float(1)\n", filename="synthetic.py"),
                               Path("synthetic.py"))
        self.assertTrue(qualified, "detector missed a qualified float() call")

    def test_adapters_do_not_execute_sql(self) -> None:
        sql_methods = {"execute", "executemany", "executescript"}
        modules: list[tuple[Path, ast.Module]] = []
        for directory in ("adapters", "cli", "client"):
            self.assertTrue((PACKAGE / directory).is_dir(), f"{directory} package must exist")
            modules.extend(walk_modules(PACKAGE / directory))
        app = PACKAGE / "app.py"
        self.assertTrue(app.is_file(), "app.py must exist")
        for path in (app, PACKAGE / "mcp.py"):
            if path.is_file():
                modules.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
        violations: list[str] = []
        for path, tree in modules:
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in sql_methods):
                    violations.append(f"{path}: line {node.lineno}: .{node.func.attr}()")
        self.assertEqual(violations, [], f"SQL execution in adapters: {violations}")

    def test_cli_secret_options_are_boolean_flags(self) -> None:
        secret_words = ("password", "secret", "token")
        # Argument parsers live in cli/*.py, but __main__.py and lifecycle/
        # also define parsers, so they are scanned with the cli modules.
        paths = [
            *sorted((PACKAGE / "cli").glob("*.py")),
            *sorted((PACKAGE / "lifecycle").glob("*.py")),
            PACKAGE / "__main__.py",
        ]
        violations: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_argument" and node.args):
                    continue
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                option = first.value
                if not any(word in option.lower() for word in secret_words):
                    continue
                action = next(
                    (kw.value for kw in node.keywords if kw.arg == "action"), None
                )
                if not (isinstance(action, ast.Constant) and action.value == "store_true"):
                    observed = ast.unparse(action) if action is not None else "<none>"
                    violations.append(
                        f"{path}: line {node.lineno}: {option} action={observed}"
                    )
        self.assertEqual(violations, [], f"secret CLI options must be boolean flags: {violations}")


if __name__ == "__main__":
    unittest.main()
