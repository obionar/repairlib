"""The checkout update helper: fast-forward only, and service discovery from a unit file."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repairlib.lifecycle.checkout import CheckoutError, parse_service_target, pull, restart_service


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


class ServiceTargetTests(unittest.TestCase):
    def test_a_user_unit_yields_the_workspace_and_port(self):
        text = ('[Service]\nExecStart=/usr/bin/python3 -m repairlib service foreground '
                '--workspace /home/owner/shop --port 8099\n')
        self.assertEqual(parse_service_target(text), (Path("/home/owner/shop"), 8099))

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


if __name__ == "__main__":
    unittest.main()
