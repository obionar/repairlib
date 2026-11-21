"""The checkout update helper: fast-forward only, and service discovery from a unit file."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from argparse import Namespace
from contextlib import redirect_stderr
from io import StringIO

from repairlib.lifecycle.checkout import CheckoutError, parse_service_target, pull, remote_commit, restart_service
from repairlib.lifecycle.service import unit_bytes
from repairlib.cli.workspace import run_update


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


class PullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.work = root / "work"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.work)], check=True)
        git(self.work, "config", "user.email", "test@example.org")
        git(self.work, "config", "user.name", "Test")
        (self.work / "file.txt").write_text("one\n", encoding="utf8")
        git(self.work, "add", "file.txt")
        git(self.work, "commit", "-q", "-m", "one")
        git(self.work, "branch", "-M", "main")
        git(self.work, "push", "-q", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.other = root / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.remote), str(self.other)], check=True)
        git(self.other, "config", "user.email", "test@example.org")
        git(self.other, "config", "user.name", "Test")

    def test_pull_fast_forwards_to_the_pushed_commit(self):
        (self.other / "file.txt").write_text("two\n", encoding="utf8")
        git(self.other, "commit", "-q", "-am", "two")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        before, after = pull(self.work)
        self.assertNotEqual(before, after)
        self.assertEqual((self.work / "file.txt").read_text(encoding="utf8"), "two\n")

    def test_a_dirty_checkout_is_refused(self):
        (self.work / "file.txt").write_text("local\n", encoding="utf8")
        with self.assertRaises(CheckoutError):
            pull(self.work)

    def test_schema_change_is_refused_before_checkout_changes(self):
        before = git(self.work, "rev-parse", "HEAD")
        migration = self.other / "src/repairlib/storage/migrations/0039_change.sql"
        migration.parent.mkdir(parents=True)
        migration.write_text("SELECT 1;\n", encoding="utf8")
        git(self.other, "add", str(migration))
        git(self.other, "commit", "-q", "-m", "migration")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(CheckoutError, "checkout was not changed"):
            pull(self.work)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), before)
        self.assertFalse((self.work / "src/repairlib/storage/migrations/0039_change.sql").exists())

    def test_check_uses_current_branch(self):
        git(self.work, "checkout", "-q", "-b", "other")
        git(self.work, "push", "-q", "origin", "other")
        (self.work / "file.txt").write_text("other\n", encoding="utf8")
        git(self.work, "commit", "-q", "-am", "other")
        git(self.work, "push", "-q", "origin", "other")
        self.assertEqual(remote_commit(self.work), git(self.work, "rev-parse", "--short=7", "HEAD"))

    def test_check_reports_missing_remote_branch(self):
        git(self.work, "checkout", "-q", "-b", "local-only")
        with self.assertRaisesRegex(CheckoutError, "does not exist on origin"):
            remote_commit(self.work)

    def test_check_reports_unreachable_origin(self):
        git(self.work, "remote", "set-url", "origin", str(self.work / "missing"))
        with self.assertRaises(CheckoutError):
            remote_commit(self.work)

    def test_check_refuses_detached_head(self):
        git(self.work, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(CheckoutError, "detached HEAD"):
            remote_commit(self.work)


class ServiceTargetTests(unittest.TestCase):
    def test_a_user_unit_yields_the_workspace_and_port(self):
        text = ('[Service]\nExecStart=/usr/bin/python3 -m repairlib service foreground '
                '--workspace /home/owner/shop --port 8099\n')
        self.assertEqual(parse_service_target(text), (Path("/home/owner/shop"), 8099))

    def test_generated_unit_yields_workspace_and_port(self):
        workspace = "/home/owner/shop 100% ready"
        text = unit_bytes(("/usr/bin/python3", "service", "foreground",
                           "--workspace", workspace, "--port", "8099")).decode()
        self.assertEqual(parse_service_target(text), (Path(workspace), 8099))

    def test_a_quoted_openrc_command_line_is_read(self):
        text = "command_args='-I -B -c ... /opt/repairlib/src service foreground " \
               "--workspace \"/home/owner/shop data\" --port 8092'\n"
        self.assertEqual(parse_service_target(text), (Path("/home/owner/shop data"), 8092))

    def test_text_without_both_options_is_ignored(self):
        self.assertIsNone(parse_service_target("ExecStart=/usr/bin/python3 -m repairlib\n"))


class RestartTests(unittest.TestCase):
    @patch("repairlib.lifecycle.checkout.subprocess.run")
    def test_reports_service_error_from_json_stdout(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '{"error":"permission denied"}', "")
        with self.assertRaisesRegex(CheckoutError, "permission denied"):
            restart_service(Path("/home/owner/shop"), 8000, True)

    @patch("repairlib.lifecycle.checkout.subprocess.run")
    def test_uses_fallback_for_invalid_json(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "not json", "")
        with self.assertRaisesRegex(CheckoutError, "service did not restart"):
            restart_service(Path("/home/owner/shop"), 8000, True)


class UpdateCliTests(unittest.TestCase):
    @patch("repairlib.lifecycle.checkout.remote_commit", side_effect=CheckoutError("remote unavailable"))
    def test_update_check_reports_error_without_traceback(self, remote):
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = run_update(Namespace(check=True))
        self.assertEqual(result, 1)
        self.assertIn("update: remote unavailable", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
