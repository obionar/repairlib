"""Gate tests for S1a.1: reproducible developer baseline."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def python_env() -> dict[str, str]:
    """Run project code from source without installing it or writing bytecode."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SRC), env.get("PYTHONPATH", "")) if part
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


async def get_health(workspace: Path):
    import httpx

    from repairlib.app import create_app

    app = create_app(workspace=workspace)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            return await client.get("/health")


class DeveloperBaselineTests(unittest.TestCase):
    def test_import_and_app_factory_create_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            code = (
                "import sys;"
                "import repairlib;"
                "from repairlib.app import create_app;"
                "create_app(workspace=sys.argv[1]);"
            )
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            proc = subprocess.run(
                [sys.executable, "-c", code, str(workspace)],
                env=python_env(),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,  # App construction takes over 10 seconds cold on the Pi.
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            self.assertEqual(before, after)
            self.assertFalse(workspace.exists())

    def test_health_reports_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            response = asyncio.run(get_health(workspace))
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertFalse(body["ready"])
            self.assertEqual(body["state"], "uninitialized")
            self.assertNotIn("workspace", body)
            self.assertFalse(workspace.exists())

    def test_doctor_missing_workspace_diagnoses_without_creating(self) -> None:
        from repairlib.doctor import diagnose_workspace

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "missing workspace"
            report = diagnose_workspace(workspace)
            self.assertEqual(report.status, "missing")
            self.assertFalse(workspace.exists())

            proc = subprocess.run(
                [sys.executable, "-m", "repairlib", "doctor", "--workspace", str(workspace)],
                env=python_env(),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("missing", proc.stdout)
            self.assertFalse(workspace.exists())

    def test_workspace_path_with_spaces(self) -> None:
        from repairlib.config import resolve_workspace

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "my workspace" / "state"
            self.assertEqual(resolve_workspace(workspace), workspace)
            response = asyncio.run(get_health(workspace))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(workspace.exists())

    def test_dev_server_binds_loopback_by_default(self) -> None:
        from repairlib.__main__ import main

        with tempfile.TemporaryDirectory() as tmp, patch("uvicorn.run") as run:
            workspace = Path(tmp) / "missing"
            self.assertEqual(main(["dev", "--workspace", str(workspace)]), 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
            self.assertEqual(run.call_args.kwargs["port"], 8000)
            self.assertFalse(workspace.exists())

    def test_doctor_refuses_a_file_as_workspace(self) -> None:
        from repairlib.doctor import diagnose_workspace

        with tempfile.TemporaryDirectory() as tmp:
            occupied = Path(tmp) / "occupied"
            occupied.write_text("existing content")
            self.assertEqual(diagnose_workspace(occupied).status, "invalid")
            self.assertEqual(occupied.read_text(), "existing content")


if __name__ == "__main__":
    unittest.main()
