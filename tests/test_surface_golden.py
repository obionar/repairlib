"""The public surfaces (HTTP API schema, CLI/MCP catalog, MCP wire definitions) must not drift silently.

Refactors of the adapter layers must leave `golden/surface.tsv` byte-identical. When a change to a
surface is intended, regenerate it with `UPDATE_GOLDEN=1` and review the diff like any API change.
"""

import json
import os
from pathlib import Path
import unittest

from repairlib.adapters import mcp as mcp_adapter
from repairlib.app import create_app
from repairlib.cli import catalog
from repairlib.__main__ import build_parser

GOLDEN = Path(__file__).with_name("golden") / "surface.tsv"


def current_surface() -> str:
    """One compact JSON entry per line, so a changed route, flag or tool is a one-line diff."""
    tools = mcp_adapter.tool_definitions()
    openapi = create_app(workspace="/nonexistent/repairlib-golden").openapi()
    entries = {f"openapi {key}": value for key, value in openapi.items() if key not in ("paths", "components")}
    entries.update({f"openapi path {path}": item for path, item in openapi["paths"].items()})
    schemas = openapi.get("components", {}).get("schemas", {})
    # pydantic 2.12 adds ctx/input to its ValidationError model; older stock packages do not.
    schemas = {name: {**item, "properties": {key: value for key, value in item.get("properties", {}).items()
                                            if key not in ("ctx", "input")}}
               if name == "ValidationError" else item
               for name, item in schemas.items()}
    entries.update({f"openapi schema {name}": item for name, item in schemas.items()})
    entries.update({f"catalog {op['surface']} {op['name']}": op
                    for op in catalog.build_catalog(build_parser(), tools)["operations"]})
    entries.update({f"mcp {tool.name}": tool.wire() for tool in tools})
    return "".join(f"{name}\t{json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
                   for name, value in sorted(entries.items()))


class SurfaceGoldenTests(unittest.TestCase):
    def test_surfaces_match_the_golden_file(self):
        text = current_surface()
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN.write_text(text, encoding="utf8")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf8"),
                         "A public surface changed. If intended, rerun with UPDATE_GOLDEN=1 and review the diff.")


if __name__ == "__main__":
    unittest.main()
