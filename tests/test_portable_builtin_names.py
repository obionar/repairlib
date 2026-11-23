"""An imported history cannot reserve the names of the built-in local administrators (audit R05)."""

from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from repairlib.client import local
from repairlib.domain.setup import initialize_workspace
from test_repairs import DEVICE
from test_tokens import PASSWORD, TokenFixture


class ImportedBuiltInNameTests(TokenFixture, unittest.TestCase):
    def test_first_cli_and_mcp_use_after_import_succeeds_and_history_stays_inactive(self):
        source = local.open_workspace(self.workspace, actor="mcp")
        local.open_workspace(self.workspace, actor="cli")
        local.create_repair(source, DEVICE, "repair")
        buffer = BytesIO()
        size = local.download_portable_shop(source, buffer)
        buffer.seek(0)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "workspace"
            initialize_workspace(path, shop="Destination", timezone="UTC", username="owner", password=PASSWORD)
            dest = local.open_workspace(path)
            local.import_portable_shop(dest, buffer, size, destination_shop_id=1, revision=1, key="import")
            for actor in ("mcp", "cli"):
                local.open_workspace(path, actor=actor)
            with closing(sqlite3.connect(path / "state" / "repairlib.sqlite3")) as db:
                imported = db.execute("SELECT username, active FROM users WHERE username LIKE 'imported-%' "
                                      "ORDER BY id").fetchall()
                self.assertEqual({row[1] for row in imported}, {0})
                self.assertGreaterEqual(len(imported), 2)
                builtin = db.execute("SELECT username, active FROM users WHERE username IN ('cli', 'mcp') "
                                     "ORDER BY username").fetchall()
                self.assertEqual(builtin, [("cli", 1), ("mcp", 1)])


if __name__ == "__main__":
    unittest.main()
