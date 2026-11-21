"""Gate test: the canonical check runner fails when any test fails."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check.py"


class DistroRunTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("repairlib_check_distro", CHECK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_distro_runs_the_same_check_in_a_stock_package_container(self) -> None:
        from unittest.mock import patch

        module = self.load()
        calls = []
        real_run = subprocess.run

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "checkout"
            real_run(["git", "clone", "-q", "--no-local", str(ROOT), str(repo)], check=True)
            (repo / ".git/info/exclude").write_text("/audit-sentinel\n", encoding="utf-8")
            (repo / "audit-sentinel").touch()
            (repo / "README.md").write_text("uncommitted edit", encoding="utf-8")

            def git(path, *args):
                return subprocess.check_output(["git", "-C", str(path), *args])

            def run(argv, **kwargs):
                if argv[0] != "podman":
                    return real_run(argv, **kwargs)
                calls.append(argv)
                if argv[1] == "run":
                    mount = Path(argv[argv.index("-v") + 1].removesuffix(":/orig:ro,Z"))
                    self.assertNotEqual(mount, repo)
                    self.assertEqual(Path(calls[0][-1]), mount / "scripts/containers")
                    self.assertFalse((mount / "audit-sentinel").exists())
                    self.assertEqual((mount / "README.md").read_bytes(), git(repo, "show", "HEAD:README.md"))
                    self.assertEqual(git(mount, "rev-parse", "HEAD", "HEAD^"),
                                     git(repo, "rev-parse", "HEAD", "HEAD^"))
                return subprocess.CompletedProcess(argv, 0)

            with patch.object(module, "ROOT", repo), \
                    patch.object(module.shutil, "which", return_value="/usr/bin/podman"), \
                    patch.object(module.subprocess, "run", side_effect=run):
                self.assertEqual(module.main(["--jobs", "4", "--distro", "alpine", "--fast"]), 0)
        build, launch = calls
        self.assertEqual(build[:2], ["podman", "build"])
        self.assertTrue(build[build.index("-f") + 1].endswith("scripts/containers/alpine.Containerfile"))
        self.assertEqual(launch[-3:], ["--jobs", "4", "--fast"])
        self.assertNotIn("--distro", launch)
        self.assertIn("python3 scripts/check.py", launch[launch.index("-c") + 1])

    def test_containerfiles_use_only_distribution_packages(self) -> None:
        for name in ("debian", "alpine"):
            lines = (ROOT / "scripts/containers" / f"{name}.Containerfile").read_text().splitlines()
            text = "\n".join(line for line in lines if not line.startswith("#"))
            for forbidden in ("pip", "venv", "uv ", "curl", "wget"):
                self.assertNotIn(forbidden, text, f"{name}: {forbidden}")


class CheckRunnerTests(unittest.TestCase):
    def test_check_runner_refuses_empty_test_selections(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "-B", str(CHECK), "--tests", tmp]
            for args in ([], ["--jobs", "2"], ["--module", "test_missing.py"]):
                with self.subTest(args=args):
                    result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("No tests discovered", result.stderr)

    def test_check_runner_exits_nonzero_on_failure(self) -> None:
        spec = importlib.util.spec_from_file_location("repairlib_check", CHECK)
        self.assertIsNotNone(spec and spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        with tempfile.TemporaryDirectory() as tmp:
            failing = Path(tmp) / "test_intentional_failure.py"
            failing.write_text(
                "import unittest\n"
                "class Intentional(unittest.TestCase):\n"
                "    def test_fails(self):\n"
                "        self.fail('intentional failure')\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertNotEqual(module.run_tests(Path(tmp)), 0)

    def test_parallel_modules_overlap_and_a_failure_does_not_drop_other_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, other in (("a", "b"), ("b", "a"), ("c", "a")):
                (root / f"test_{name}.py").write_text(
                    "import os, time, unittest\nfrom pathlib import Path\n"
                    "class Worker(unittest.TestCase):\n"
                    f"    def test_{name}(self):\n"
                    f"        Path(__file__).with_name('{name}').write_text(str(os.getpid()))\n"
                    f"        peer = Path(__file__).with_name('{other}')\n"
                    "        deadline = time.monotonic() + 5\n"
                    "        while not peer.exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
                    "        self.assertTrue(peer.exists(), 'modules did not overlap')\n"
                    + ("        self.fail('parallel failure')\n" if name == "a" else ""),
                    encoding="utf-8",
                )
            (root / "test_broken.py").write_text("raise ImportError('broken test import')\n", encoding="utf-8")
            command = [sys.executable, "-B", str(CHECK), "--tests", tmp, "--jobs", "2"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("parallel failure", result.stderr)
            self.assertIn("broken test import", result.stderr)
            self.assertNotIn("modules did not overlap", result.stderr)
            pids = {(root / name).read_text() for name in ("a", "b", "c")}
            self.assertEqual(len(pids), 3)

    def test_workspace_template_reuses_setup_without_sharing_writes(self):
        from contextlib import closing
        import sqlite3
        from unittest.mock import patch

        from repairlib.lifecycle.workspace import database_path, workspace_status
        from support.workspace import clone_workspace, initialize_workspace

        with tempfile.TemporaryDirectory() as tmp, patch(
            "support.workspace.initialize_workspace", wraps=initialize_workspace,
        ) as initialize:
            first, second = (Path(tmp) / name for name in ("first", "second"))
            for workspace in (first, second):
                clone_workspace(workspace, "synthetic template isolation passphrase")
                self.assertEqual(workspace_status(workspace), "initialized")
            self.assertEqual(initialize.call_count, 1)
            with closing(sqlite3.connect(database_path(first))) as db, db:
                db.execute("UPDATE shops SET name = 'Changed copy'")
            with closing(sqlite3.connect(database_path(second))) as db:
                self.assertEqual(db.execute("SELECT name FROM shops").fetchone(), ("Synthetic",))

    def test_fast_tier_excludes_slow_modules_without_changing_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("architecture", "baseline", "check_runner", "operability"):
                (root / f"test_{name}.py").write_text(
                    "import unittest\nclass Smoke(unittest.TestCase):\n"
                    f"    def test_{name}(self): pass\n", encoding="utf-8",
                )
            (root / "test_slow.py").write_text(
                "import unittest\nclass Slow(unittest.TestCase):\n"
                "    def test_slow(self): self.fail('full-only failure')\n", encoding="utf-8",
            )
            command = [sys.executable, "-B", str(CHECK), "--tests", tmp]
            fast = subprocess.run([*command, "--fast"], capture_output=True, text=True, timeout=15)
            self.assertEqual(fast.returncode, 0, fast.stderr)
            self.assertIn("Ran 4 tests", fast.stderr)
            self.assertNotIn("test_slow", fast.stderr)
            full = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(full.returncode, 1, full.stderr)
            self.assertIn("full-only failure", full.stderr)
            (root / "test_architecture.py").unlink()
            missing = subprocess.run([*command, "--fast"], capture_output=True, text=True, timeout=15)
            self.assertEqual(missing.returncode, 1, missing.stderr)
            self.assertIn("test_architecture.py", missing.stderr)


if __name__ == "__main__":
    unittest.main()
