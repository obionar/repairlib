"""Checkout updates against disposable Git repositories and real SQLite workspaces."""

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from argparse import Namespace
from contextlib import redirect_stderr
from io import StringIO

from repairlib.lifecycle.backup_archive import restore_archive, verify_backup
from repairlib.lifecycle.checkout import (
    PENDING, CheckoutError, checkout_state, fetch_update, parse_service_target, remote_commit, service_command,
)
from repairlib.lifecycle.checkout_update import run_candidate, update_checkout
from repairlib.lifecycle.service import BOOT, foreground, foreground_args, openrc_script, python_args, unit_bytes
from repairlib.lifecycle.workspace import WorkspaceError, create_workspace, database_path, maintenance_lock
from repairlib.storage.initialization import MIGRATIONS, SCHEMA_VERSION
from repairlib.cli.workspace import run_update


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        # Pin the bare HEAD before cloning so the clone's default branch is main on any git.
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.work = root / "work"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.work)], check=True)
        git(self.work, "config", "user.email", "test@example.org")
        git(self.work, "config", "user.name", "Test")
        (self.work / "file.txt").write_text("one\n", encoding="utf8")
        git(self.work, "add", "file.txt")
        git(self.work, "commit", "-q", "-m", "one")
        git(self.work, "branch", "-M", "main")
        git(self.work, "push", "-q", "origin", "main")
        self.other = root / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.remote), str(self.other)], check=True)
        git(self.other, "config", "user.email", "test@example.org")
        git(self.other, "config", "user.name", "Test")


class GitTests(GitFixture):
    def test_fetch_pins_the_pushed_commit_without_changing_the_checkout(self):
        (self.other / "file.txt").write_text("two\n", encoding="utf8")
        git(self.other, "commit", "-q", "-am", "two")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        branch, before = checkout_state(self.work)
        after = fetch_update(self.work, branch, before)
        self.assertNotEqual(before, after)
        self.assertEqual(after, git(self.other, "rev-parse", "HEAD"))
        self.assertEqual((self.work / "file.txt").read_text(encoding="utf8"), "one\n")

    def test_a_dirty_checkout_is_refused(self):
        (self.work / "file.txt").write_text("local\n", encoding="utf8")
        with self.assertRaises(CheckoutError):
            update_checkout(self.work)

    def test_diverged_checkout_is_untouched(self):
        git(self.work, "commit", "--allow-empty", "-qm", "local")
        git(self.other, "commit", "--allow-empty", "-qm", "origin")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        before = git(self.work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(CheckoutError, "diverged"):
            update_checkout(self.work)
        self.assertEqual(before, git(self.work, "rev-parse", "HEAD"))

    def test_ahead_checkout_does_not_access_service(self):
        git(self.work, "commit", "--allow-empty", "-qm", "local")
        with patch("repairlib.lifecycle.checkout_update.service_target") as discover:
            self.assertEqual(update_checkout(self.work)[2:], ("unchanged", None))
            discover.assert_not_called()

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

    def test_generated_openrc_script_round_trips(self):
        workspace = Path("/home/owner/owner's shop")
        command = python_args(Path("/usr/bin/python3"), Path("/home/owner/src"), BOOT)
        script = openrc_script(command + foreground_args(workspace, 8092), "owner", "owner")
        self.assertEqual(parse_service_target(script.decode()), (workspace, 8092))

    def test_text_without_both_options_is_ignored(self):
        self.assertIsNone(parse_service_target("ExecStart=/usr/bin/python3 -m repairlib\n"))


class ServiceCommandTests(unittest.TestCase):
    @patch("repairlib.lifecycle.checkout.subprocess.run")
    def test_reports_service_error_from_json_stdout(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '{"error":"permission denied"}', "")
        with self.assertRaisesRegex(CheckoutError, "permission denied"):
            service_command(Path("/home/owner"), "start", (Path("/home/owner/shop"), 8000, True))

    @patch("repairlib.lifecycle.checkout.subprocess.run")
    def test_uses_fallback_for_invalid_json(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "not json", "")
        with self.assertRaisesRegex(CheckoutError, "Could not confirm service start"):
            service_command(Path("/home/owner"), "start", (Path("/home/owner/shop"), 8000, True))


class UpdateFlowTests(GitFixture):
    def setUp(self):
        super().setUp()
        source = Path(__file__).resolve().parents[1]
        for name in ("bin", "src"):
            shutil.copytree(source / name, self.work / name, ignore=shutil.ignore_patterns("__pycache__"))
        (self.work / ".gitignore").write_text("/data/\n", encoding="utf8")
        git(self.work, "add", ".")
        git(self.work, "commit", "-qm", "installed code")
        git(self.work, "push", "-q")
        git(self.other, "pull", "-q", "--ff-only")
        self.before = git(self.work, "rev-parse", "HEAD")
        self.workspace = self.work / "data"
        create_workspace(self.workspace, "Test shop", "UTC", "admin", "unused")
        self.pending = self.work / ".git" / PENDING
        self.service = "running"
        self.calls = []
        self.mock_service = self.enterContext(patch(
            "repairlib.lifecycle.checkout_update.service_command", side_effect=self.supervisor))
        self.enterContext(patch("repairlib.lifecycle.checkout_update.service_target",
                                return_value=(self.workspace, 8081, True)))

    def supervisor(self, repo, action, target):
        self.calls.append(action)
        if action == "stop":
            self.assertTrue(self.pending.is_file())
            self.service = "stopped"
        elif action == "start":
            self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION + 1)
            self.service = "running"
        return {"service": self.service, "health": "ready" if self.service == "running" else None}

    def schema(self, workspace):
        with closing(sqlite3.connect(database_path(workspace))) as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    def publish_migration(self, sql="CREATE TABLE update_probe (value TEXT);\n"):
        storage = self.other / "src" / "repairlib" / "storage"
        initialization = storage / "initialization.py"
        anchor = f'    "{MIGRATIONS[-1]}",\n'
        migration = f"{SCHEMA_VERSION + 1:04d}_update_probe.sql"
        initialization.write_text(initialization.read_text().replace(anchor, anchor + f'    "{migration}",\n'))
        (storage / "migrations" / migration).write_text(sql)
        git(self.other, "add", ".")
        git(self.other, "commit", "-qm", "new migration")
        git(self.other, "push", "-q")
        return git(self.other, "rev-parse", "HEAD")

    def test_runs_real_restore_and_migration_with_the_pinned_python_code(self):
        after = self.publish_migration()
        old, new, service, backup = update_checkout(self.work)
        self.assertEqual((old, new, service), (self.before, after, "ready"))
        self.assertEqual(self.calls, ["status", "stop", "status", "start"])
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION + 1)
        self.assertEqual(verify_backup(backup), ("Backup archive", SCHEMA_VERSION))
        restore = backup.parent / "restored"
        restore_archive(backup, restore)
        self.assertEqual(self.schema(restore), SCHEMA_VERSION)
        self.assertFalse(self.pending.exists())
        self.assertFalse(list(backup.parent.glob("rehearsal-*")))

    def test_a_stopped_service_stays_stopped(self):
        self.service = "stopped"
        self.publish_migration()
        self.assertEqual(update_checkout(self.work)[2], "left stopped")
        self.assertEqual(self.calls, ["status", "status"])
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION + 1)

    def test_service_identity_failure_is_before_stop(self):
        self.publish_migration()
        self.mock_service.side_effect = CheckoutError("Refusing an unmanaged service unit.")
        with self.assertRaisesRegex(CheckoutError, "unmanaged service"):
            update_checkout(self.work)
        self.mock_service.assert_called_once_with(self.work, "status", (self.workspace, 8081, True))
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.before, git(self.work, "rev-parse", "HEAD"))

    def test_incoming_tracked_workspace_path_is_refused_before_stop(self):
        (self.other / "data").write_text("not a workspace")
        git(self.other, "add", "-f", "data")
        git(self.other, "commit", "-qm", "conflicting path")
        git(self.other, "push", "-q")
        with self.assertRaisesRegex(CheckoutError, "overlaps the workspace path"):
            update_checkout(self.work)
        self.mock_service.assert_not_called()
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION)

    def test_failed_rehearsal_leaves_head_and_live_schema_alone(self):
        self.publish_migration("NOT A VALID MIGRATION;\n")
        with self.assertRaisesRegex(CheckoutError, "Stopped at rehearse restore"):
            update_checkout(self.work)
        self.assertEqual(self.calls, ["status", "stop"])
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION)
        self.assertEqual(self.before, git(self.work, "rev-parse", "HEAD"))
        record = json.loads(self.pending.read_text())
        self.assertEqual(verify_backup(Path(record["backup"]))[1], SCHEMA_VERSION)
        with self.assertRaisesRegex(CheckoutError, "previous update did not finish"):
            update_checkout(self.work)
        with patch("repairlib.lifecycle.service.SOURCE", self.work / "src"):
            with self.assertRaisesRegex(WorkspaceError, "Checkout update is pending"):
                foreground(self.workspace, 8081)

    def test_other_writer_blocks_the_update_before_backup_or_cutover(self):
        self.publish_migration()
        with maintenance_lock(self.workspace, exclusive=False, create=False):
            with self.assertRaisesRegex(CheckoutError, "app is running"):
                update_checkout(self.work)
        self.assertEqual(self.before, git(self.work, "rev-parse", "HEAD"))
        self.assertFalse(Path(json.loads(self.pending.read_text())["backup"]).exists())

    def test_interrupted_cutover_retains_backup_and_does_not_start(self):
        after = self.publish_migration()

        def interrupt(repo, workspace, backup, lock_fd=None):
            if workspace == self.workspace:
                self.assertIsNotNone(lock_fd)
                with self.assertRaises(WorkspaceError):
                    with maintenance_lock(workspace, exclusive=False, create=False):
                        self.fail("The live workspace lock must be held through cutover.")
                raise KeyboardInterrupt
            run_candidate(repo, workspace, backup, lock_fd)

        with patch("repairlib.lifecycle.checkout_update.run_candidate", side_effect=interrupt):
            with self.assertRaisesRegex(CheckoutError, "Stopped at migrate and run post-update doctor: Interrupted"):
                update_checkout(self.work)
        self.assertEqual(after, git(self.work, "rev-parse", "HEAD"))
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION)
        self.assertEqual(self.calls, ["status", "stop", "status"])
        self.assertTrue(Path(json.loads(self.pending.read_text())["backup"]).is_file())

    def test_failed_service_health_attempts_to_stop_and_keeps_recovery_record(self):
        self.publish_migration()

        def unready(repo, action, target):
            reply = self.supervisor(repo, action, target)
            return {"service": "unready"} if action == "start" else reply

        self.mock_service.side_effect = unready
        with self.assertRaisesRegex(CheckoutError, "listener and health"):
            update_checkout(self.work)
        self.assertEqual(self.calls, ["status", "stop", "status", "start", "stop"])
        self.assertEqual(self.schema(self.workspace), SCHEMA_VERSION + 1)
        self.assertEqual(json.loads(self.pending.read_text())["stage"], "service start or verification failed")


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
