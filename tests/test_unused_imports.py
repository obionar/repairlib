"""A6: no unused imports under src/ (stdlib-only lint; no linter dependency)."""

import ast
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
# Names other modules import through these ones.
RE_EXPORTS = {("domain/customers.py", "StorageError"), ("domain/customers.py", "StorageOperationalError"),
              ("domain/tokens.py", "TokenConflict")}


class UnusedImportTests(unittest.TestCase):
    def test_every_import_in_src_is_used(self):
        unused = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            text = path.read_text()
            tree = ast.parse(text)
            used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            relative = path.relative_to(SRC / "repairlib").as_posix()
            for node in tree.body:
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for alias in node.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    if (name in used or name == "annotations" or (relative, name) in RE_EXPORTS
                            or f'"{name}' in text or f"'{name}" in text):
                        continue
                    unused.append(f"{relative}:{node.lineno} {name}")
        self.assertEqual(unused, [], "unused imports")


if __name__ == "__main__":
    unittest.main()
