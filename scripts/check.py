#!/usr/bin/env python3
"""Single cross-platform check entry point (standard library only)."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
FAST_MODULES = ("test_architecture.py", "test_baseline.py", "test_check_runner.py", "test_operability.py")
# Real installs, clones and service starts (~2 minutes): run with --full for installer changes and releases.
SLOW_MODULES = ("test_linux_installer.py",)


def run_tests(tests_dir: Path = TESTS, *, fast: bool = False, full: bool = False, pattern: str = "test*.py") -> int:
    """Discover and run tests under *tests_dir*; return a process exit code."""
    source = str(SRC)
    if source not in sys.path:
        sys.path.insert(0, source)
    loader = unittest.TestLoader()
    patterns = FAST_MODULES if fast else (pattern,)
    if not fast and not full and pattern == "test*.py":
        patterns = tuple(sorted(path.name for path in tests_dir.glob(pattern) if path.name not in SLOW_MODULES)) or patterns
    suite = unittest.TestSuite()
    for name in patterns:
        selected = loader.discover(str(tests_dir), pattern=name, top_level_dir=str(tests_dir))
        if not selected.countTestCases():
            print(f"No tests discovered for {name}", file=sys.stderr)
            return 1
        suite.addTests(selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_parallel(tests_dir: Path, jobs: int, full: bool = False) -> int:
    entries = sorted(tests_dir.iterdir())
    # shortcut: shard flat suites only, use serial discovery when packages are added.
    if any((entry / "__init__.py").is_file() for entry in entries if entry.is_dir()):
        print("Test packages found; using serial discovery.", file=sys.stderr)
        return run_tests(tests_dir, full=full)
    # Largest file first keeps the slow modules from starting last (near the best schedule, no state to keep).
    modules = sorted((entry.name for entry in entries if entry.is_file() and entry.match("test*.py")
                      and (full or entry.name not in SLOW_MODULES)),
                     key=lambda name: -(tests_dir / name).stat().st_size)
    if not modules:
        return run_tests(tests_dir, full=full)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--tests", str(tests_dir)]
    failures = 0
    with ThreadPoolExecutor(max_workers=min(jobs, len(modules))) as pool:
        workers = {
            pool.submit(subprocess.run, [*command, "--module", module], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, errors="replace"): module
            for module in modules
        }
        for worker in as_completed(workers):
            result = worker.result()
            print(f"\n--- {workers[worker]} (exit {result.returncode}) ---", file=sys.stderr)
            print(result.stdout, end="", file=sys.stderr, flush=True)
            failures += result.returncode != 0
    print(f"Parallel check: {len(modules) - failures}/{len(modules)} modules passed.", file=sys.stderr)
    return int(failures != 0)


def run_in_distro(distro: str, forwarded: list[str]) -> int:
    """Check committed HEAD inside a stock-package Debian or Alpine container."""
    if shutil.which("podman") is None:
        print("check: --distro needs podman.", file=sys.stderr)
        return 1
    if shutil.which("git") is None:
        print("check: --distro needs git.", file=sys.stderr)
        return 1
    image = f"localhost/repairlib-stock-{distro}"
    with TemporaryDirectory(prefix="repairlib-distro-") as temp:
        source = Path(temp) / "source"
        clone = subprocess.run(["git", "clone", "--quiet", "--no-local", "--single-branch",
                                "--no-tags", str(ROOT), str(source)])
        if clone.returncode:
            return clone.returncode
        context = source / "scripts/containers"
        build = subprocess.run(["podman", "build", "-q", "-t", image, "-f",
                                str(context / f"{distro}.Containerfile"), str(context)],
                               stdout=subprocess.DEVNULL)
        if build.returncode:
            return build.returncode
        return subprocess.run(
            ["podman", "run", "--rm", "--userns=keep-id", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
             "-v", f"{source}:/orig:ro,Z", "--tmpfs", "/work:rw,exec,mode=1777,size=1g", "-w", "/work",
             image, "sh", "-c", 'git config --global --add safe.directory /work && '
             'cp -R /orig/. /work/ && python3 scripts/check.py "$@"', "sh", *forwarded]).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RepairLib checks")
    parser.add_argument("--tests", type=Path, default=TESTS, help=argparse.SUPPRESS)
    parser.add_argument("--fast", action="store_true", help="run the local smoke suite instead of all tests")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="run up to N test modules in parallel (default: one per CPU)")
    parser.add_argument("--full", action="store_true", help="also run the slow installer integration tests")
    parser.add_argument("--module", help="run one test module, for example test_search.py")
    parser.add_argument("--distro", choices=("debian", "alpine"),
                        help="check committed HEAD with that distribution's stock packages in Podman")
    args = parser.parse_args(argv)
    if args.distro:
        forwarded = [item for item in (argv if argv is not None else sys.argv[1:])]
        index = forwarded.index("--distro")
        return run_in_distro(args.distro, forwarded[:index] + forwarded[index + 2:])
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.jobs > 1 and not args.fast and args.module is None:
        return run_parallel(args.tests, args.jobs, args.full)
    pattern = Path(args.module).name if args.module else "test*.py"
    return run_tests(args.tests, fast=args.fast, full=args.full, pattern=pattern)


if __name__ == "__main__":
    raise SystemExit(main())
