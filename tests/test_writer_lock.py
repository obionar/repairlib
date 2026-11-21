"""Exercise the checkout writer lock through its command-line entry point."""

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path("scripts/writer_lock.py")
LOCK = ".writer.lock"


class WriterLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        (self.checkout / SCRIPT.parent).mkdir()
        shutil.copyfile(ROOT / SCRIPT, self.checkout / SCRIPT)
        self.lock = self.checkout / LOCK

    def command(self, code):
        return [sys.executable, "-B", str(self.checkout / SCRIPT), "--",
                sys.executable, "-B", "-c", code]

    def run_writer(self, code):
        return subprocess.run(self.command(code), cwd=self.checkout / "scripts",
                              capture_output=True, text=True, timeout=5)

    def start_writer(self):
        holder = subprocess.Popen(
            self.command("print('ready', flush=True); input()"),
            cwd=self.checkout / "scripts", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )

        def cleanup():
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=5)

        self.addCleanup(cleanup)
        ready, _, _ = select.select([holder.stdout], [], [], 5)
        self.assertTrue(ready, "first writer failed to signal readiness")
        self.assertEqual(holder.stdout.readline(), "ready\n")
        return holder

    def test_refuses_a_second_writer_and_names_the_holder(self):
        holder = self.start_writer()
        metadata = json.loads(self.lock.read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], holder.pid)
        self.assertGreater(metadata["timestamp"], 0)

        contender = self.run_writer("print('second command ran')")
        self.assertNotEqual(contender.returncode, 0)
        self.assertIn(f"PID {holder.pid}", contender.stderr)
        self.assertEqual(contender.stdout, "")

    def test_releases_after_command_exit(self):
        holder = self.start_writer()
        _, stderr = holder.communicate("done\n", timeout=5)
        self.assertEqual(holder.returncode, 0, stderr)

        later = self.run_writer("raise SystemExit(7)")
        self.assertEqual(later.returncode, 7, later.stderr)
        later = self.run_writer("print('acquired')")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(later.stdout, "acquired\n")

    def test_reclaims_record_older_than_the_named_live_process(self):
        self.lock.write_text(json.dumps({"pid": os.getpid(), "timestamp": 1}), encoding="utf-8")
        holder = self.start_writer()
        metadata = json.loads(self.lock.read_text(encoding="utf-8"))
        self.assertEqual(metadata["pid"], holder.pid)
        self.assertGreater(metadata["timestamp"], 1)

    def test_reclaims_after_holder_is_killed(self):
        holder = self.start_writer()
        holder.kill()
        holder.communicate(timeout=5)

        later = self.run_writer("print('acquired')")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(later.stdout, "acquired\n")

    def test_lock_is_in_checkout_and_ignored_by_git(self):
        self.start_writer()
        self.assertTrue(self.lock.is_file())
        self.assertFalse((self.checkout / "scripts" / LOCK).exists())
        ignored = subprocess.run(["git", "check-ignore", "-v", "--", LOCK], cwd=ROOT,
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertIn(".gitignore:", ignored.stdout)
        self.assertTrue(ignored.stdout.endswith(f"\t{LOCK}\n"), ignored.stdout)
