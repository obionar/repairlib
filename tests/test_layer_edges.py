"""Ratchet on the package import graph: new cross-package edges must be a deliberate decision."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "repairlib"

# Intended edges, leaf packages first. A new edge fails the test; add it here only after
# deciding it is a real dependency, never to silence a cycle.
ALLOWED = {
    ("__main__", t) for t in ("adapters", "cli", "client", "config", "lifecycle")
} | {
    ("adapters", t) for t in ("client", "contracts", "domain", "i18n", "lifecycle", "money", "search")
} | {
    ("app", t) for t in ("adapters", "config", "domain", "i18n", "lifecycle", "storage")
} | {
    ("cli", t) for t in ("app", "client", "config", "contracts", "doctor", "domain", "lifecycle", "money")
} | {
    ("client", t) for t in ("config", "contracts", "domain", "lifecycle")
} | {
    ("doctor", t) for t in ("config", "contracts", "lifecycle", "storage")
} | {
    ("domain", t) for t in ("config", "contracts", "i18n", "money", "paths", "search", "storage")
} | {
    ("lifecycle", t) for t in ("config", "domain", "paths", "storage")
} | {
    ("storage", t) for t in ("contracts", "money", "paths", "search")
}
# Known layering debt (see .notes/audits/2026-11-20-architecture-audit.md, AR-1 to AR-4).
# Each edge points up or sideways and creates an import cycle; remove entries as they are fixed.
DEBT = {
    ("domain", "lifecycle"),    # AR-1 remainder: domain calls maintenance_lock, create_workspace, backup helpers
    ("lifecycle", "app"),       # AR-2: update/service build the app
    ("lifecycle", "__main__"),  # AR-2: update/service re-enter the CLI
    ("lifecycle", "doctor"),    # AR-2
    ("cli", "adapters"),        # AR-3: cli.catalog lists MCP tools
}


def edges(root: Path) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        source = parts[0] if len(parts) > 1 else path.stem
        base = ["repairlib", *parts[:-1]]
        for node in ast.walk(ast.parse(path.read_text())):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = base[: len(base) - (node.level - 1)]
                    module = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                if module == "repairlib":
                    modules = [f"repairlib.{alias.name}" for alias in node.names]
                elif module.startswith("repairlib."):
                    modules = [module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names if a.name.startswith("repairlib.")]
            for module in modules:
                target = module.split(".")[1]
                if target != source:
                    found.setdefault((source, target), f"{path.relative_to(root)}:{node.lineno}")
    return found


class LayerEdges(unittest.TestCase):
    def test_no_new_package_edges(self) -> None:
        new = {edge: where for edge, where in edges(PACKAGE).items() if edge not in ALLOWED | DEBT}
        self.assertEqual(new, {}, "new cross-package import; see tests/test_layer_edges.py")

    def test_debt_entries_still_exist(self) -> None:
        stale = DEBT - set(edges(PACKAGE))
        self.assertEqual(stale, set(), "debt fixed: delete these entries from DEBT")

    def test_detector_flags_synthetic_edge(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "storage").mkdir()
            (root / "storage" / "x.py").write_text("from repairlib.domain import y\n")
            self.assertEqual(set(edges(root)), {("storage", "domain")})


if __name__ == "__main__":
    unittest.main()
