"""Fresh installer contracts without network or a live supervisor."""

import importlib.util
from contextlib import closing, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from socketserver import StreamRequestHandler, UnixStreamServer
from threading import Thread
import unittest
from time import monotonic, sleep, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from repairlib.domain import sessions, tokens
from repairlib.domain.setup import initialize_demo_workspace
from repairlib.lifecycle.backup_archive import verify_backup
from repairlib.lifecycle.workspace import database_path, maintenance_lock
from repairlib.storage.initialization import SCHEMA_VERSION, initialize_database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install.py"


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0 and shutil.which("git"),
                     "unprivileged Linux with Git")
class TargetParentTests(unittest.TestCase):
    def test_a_missing_target_parent_is_created(self):
        spec = importlib.util.spec_from_file_location("repairlib_installer", ROOT / "scripts" / "install.py")
        self.assertIsNotNone(spec)
        loader = spec.loader
        assert loader is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "opt" / "repairlib"
            module.validate_target(target, ROOT)
            self.assertTrue(target.parent.is_dir())


class LinuxInstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Linux private installer is missing")
        spec = importlib.util.spec_from_file_location("private_installer", SCRIPT)
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "local repo"
        subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-checkout", ROOT, self.repo],
                       check=True, capture_output=True)
        # Keep the fixture tree static: no detached gc/maintenance may write into
        # it while TemporaryDirectory cleanup walks it (audit A15).
        for key, value in (("gc.auto", "0"), ("gc.autoDetach", "false"),
                           ("maintenance.auto", "false")):
            self.git("config", key, value)
        self.git("checkout", "--quiet", "--detach")
        installed_script = self.repo / "scripts/install.py"
        shutil.copyfile(SCRIPT, installed_script)
        self.git("add", "scripts/install.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "--quiet", "--allow-empty", "-m", "Installer fixture")
        self.commit = self.git("rev-parse", "HEAD").strip().decode()
        self.target = self.root / r"""install "quoted" ' %n $HOME \ files"""

    def git(self, *args):
        return subprocess.check_output(["git", "-C", self.repo, *args])

    def fixture_commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    @staticmethod
    def local_runtime(generation):
        """Stand-in for `provision`: the stock interpreter and packages need no staging."""

    def test_update_preflight_reports_local_blockers_without_writing(self):
        script = self.repo / "scripts/install.py"
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, self.target)
        forward = self.fixture_commit("Preflight candidate")
        ancestor = self.git("rev-parse", "HEAD^^").decode().strip()
        descriptor = self.target / "current.json"
        before = set(self.target.iterdir()), self.installer.generation_fingerprint(self.target)
        check = self.installer.check_update
        with patch.object(self.installer, "OPENRC_SERVICE", self.root / "not installed"), \
                patch.object(self.installer, "update_worker", side_effect=AssertionError("worker started")):
            report = check(script, forward, self.target)
            self.assertEqual(report, {"installed_commit": self.commit, "selected_commit": None,
                                      "requested_commit": forward, "phase": "original", "blocker": None})
            self.assertIn("same", check(script, self.commit, self.target)["blocker"])
            self.assertIn("descendant", check(script, ancestor, self.target)["blocker"])
            self.assertIn("local", check(script, "no-such-commit", self.target)["blocker"])
            self.assertEqual((set(self.target.iterdir()), self.installer.generation_fingerprint(self.target)), before)
            descriptor.write_text(json.dumps({"format": 1, "state": "open", "managed": {}, "stage": {
                "source_install": str(self.target), "source_commit": self.commit, "candidate_commit": forward}}))
            report = check(script, forward, self.target)
            self.assertEqual((report["phase"], report["selected_commit"]), ("open", forward))
            self.assertIn("already exists", report["blocker"])
            descriptor.write_text("{}")
            self.assertIn("malformed", check(script, forward, self.target)["blocker"])
            descriptor.unlink()
            self.installer.OPENRC_SERVICE.touch()
            self.assertIn("OpenRC", check(script, forward, self.target)["blocker"])
        result = subprocess.run([sys.executable, "-I", "-B", str(script), "--check-update",
                                 "--target", str(self.target), "--commit", "no-such-commit"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("local", json.loads(result.stdout)["blocker"])

    def test_update_stages_exact_forward_commit_and_rehearses_recovery(self):
        script = self.repo / "scripts/install.py"

        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, self.target)
        workspace = self.root / "stopped workspace"
        database = database_path(workspace)
        workspace.mkdir(mode=0o700)
        database.parent.mkdir(mode=0o700)
        (workspace / ".maintenance.lock").touch(mode=0o600)
        initialize_database(database, "Synthetic shop", "UTC", "owner", "unused", demo=True)
        original = b"synthetic attachment\x00\xff"
        name = "1" * 32
        blob = database.parent / "files" / "shop-1" / name
        blob.parent.parent.mkdir(mode=0o700)
        blob.parent.mkdir(mode=0o700)
        blob.write_bytes(original)
        blob.chmod(0o600)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("INSERT INTO repairs(shop_id, customer_id, reported_issue) VALUES (1, 1, 'Test')")
            db.execute(
                """INSERT INTO attachments
                   (shop_id, repair_id, uploader_id, uploader_username, filename, media_type,
                    size_bytes, sha256, storage_name)
                   VALUES (1, 1, 1, 'owner', 'sample', 'application/octet-stream', ?, ?, ?)""",
                (len(original), hashlib.sha256(original).hexdigest(), name),
            )
        # A stopped writer left a committed WAL with no shared-memory sidecar.
        # Staging must read the WAL and leave the old directory exactly as found.
        subprocess.run([sys.executable, "-I", "-B", "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA wal_autocheckpoint = 0")
with db:
    db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Committed WAL fixture')")
os._exit(0)
""", str(database)], check=True, timeout=15)
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        shm.unlink()
        self.assertGreater(wal.stat().st_size, 32)

        # Simulate the first later schema without editing any released migration.
        source = self.repo / "src/repairlib"
        for file in ("lifecycle/backup_archive.py", "lifecycle/service.py", "lifecycle/update.py",
                     "storage/recovery.py"):
            current = ROOT / "src/repairlib" / file
            if current.exists():
                shutil.copyfile(current, source / file)
        initialization = source / "storage/initialization.py"
        migration = f"{SCHEMA_VERSION + 1:04d}_update_fixture.sql"
        initialization.write_text(initialization.read_text().replace(
            ")\nSCHEMA_VERSION = len(MIGRATIONS)", f'    "{migration}",\n)\nSCHEMA_VERSION = len(MIGRATIONS)', 1))
        migration_path = source / "storage/migrations" / migration
        migration_body = "ALTER TABLE customers ADD COLUMN update_fixture INTEGER NOT NULL DEFAULT 1;\n"
        migration_path.write_text(migration_body)
        forward = self.fixture_commit("Forward fixture")
        migration_path.write_text("ALTER TABLE missing_fixture_table ADD COLUMN value INTEGER;\n")
        broken = self.fixture_commit("Broken forward migration")
        old_migration = source / "storage/migrations/0001_workspace.sql"
        with old_migration.open("a") as file:
            file.write("\n-- Changed applied history.\n")
        incompatible = self.fixture_commit("Incompatible migration history")
        self.git("branch", "requested", forward)
        candidate = self.root / "candidate"
        failed = self.root / "failed candidate"
        active_files = {file: file.read_bytes() for file in (self.target / "install.json",
                                                             self.target / "repairlib", database, wal, blob)}

        stage = self.installer.stage_update
        ancestor = self.git("rev-parse", f"{self.commit}^").decode().strip()
        with patch.object(self.installer, "provision", side_effect=self.local_runtime) as provision:
            for revision, reason in ((self.commit, "same"), (ancestor, "descendant")):
                with self.assertRaisesRegex(self.installer.InstallError, reason):
                    stage(script, revision, self.target, workspace, candidate)
            provision.assert_not_called()
            # A fake supervisor represents a running app with its normal shared lock.
            with maintenance_lock(workspace, exclusive=False, create=False):
                with self.assertRaisesRegex(self.installer.InstallError, "app is running"):
                    stage(script, forward, self.target, workspace, candidate)
            self.assertFalse(candidate.exists())
            with self.assertRaisesRegex(self.installer.InstallError, "preserve all installed migrations"):
                stage(script, incompatible, self.target, workspace, candidate)
            self.assertFalse(candidate.exists())
            with self.assertRaisesRegex(self.installer.InstallError, "migration"):
                stage(script, broken, self.target, workspace, failed)
            self.assertFalse((failed / "stage.json").exists())
            self.assertEqual(verify_backup(failed / "backup.zip"), ("Backup archive", SCHEMA_VERSION))

        def move_ref_during_provision(generation):
            self.git("branch", "-f", "requested", broken)
            with self.assertRaisesRegex(self.installer.InstallError, "installation lock"):
                stage(script, forward, self.target, workspace, self.root / "concurrent candidate")
            self.local_runtime(generation)

        with patch.object(self.installer, "provision", side_effect=move_ref_during_provision):
            report = stage(script, "requested", self.target, workspace, candidate)
        self.assertEqual(report, json.loads((candidate / "stage.json").read_text()))
        self.assertEqual(report["candidate_commit"], forward)
        self.assertEqual(report["source_commit"], self.commit)
        self.assertEqual(report["source_schema"], SCHEMA_VERSION)
        self.assertEqual(report["candidate_schema"], SCHEMA_VERSION + 1)
        self.assertIn("synthetic-customer-readback", report["checks"])
        self.assertEqual(report["python"]["mode"], "system")
        self.assertNotIn("uv", report)
        self.assertEqual(verify_backup(candidate / "backup.zip"), ("Backup archive", SCHEMA_VERSION))
        self.assertEqual((candidate / "workspace/state/files/shop-1" / name).read_bytes(), original)
        self.assertEqual({file: file.read_bytes() for file in active_files}, active_files)
        self.assertFalse(shm.exists())
        with closing(sqlite3.connect(database_path(candidate / "workspace"))) as db:
            self.assertEqual(db.execute("SELECT update_fixture FROM customers").fetchall(), [(1,)] * 4)
            self.assertEqual(db.execute("SELECT count(*) FROM customers WHERE name = 'Committed WAL fixture'")
                             .fetchone(), (1,))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        result = subprocess.run([candidate / "repairlib", "doctor", "--workspace", candidate / "workspace"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Retained attachments checked: 1", result.stdout)

    def test_disposable_activation_and_interrupted_prewrite_recovery(self):
        script = self.repo / "scripts/install.py"

        # Run the worker from the exact committed candidate source. Inject failures
        # into this synthetic commit only, including immediate process exits at rename.
        for name in ("__main__.py", "cli/workspace.py", "lifecycle/operations.py", "lifecycle/service.py"):
            shutil.copyfile(ROOT / "src/repairlib" / name, self.repo / "src/repairlib" / name)
        update = self.repo / "src/repairlib/lifecycle/update.py"
        contents = (ROOT / "src/repairlib/lifecycle/update.py").read_text()
        update.write_text(contents.replace('if __name__ == "__main__":', """
if len(sys.argv) > 1 and sys.argv[1] == "activate":
    fault = os.environ.get("REPAIRLIB_FIXTURE_FAULT")
    if fault:
        original_health = check_health
        original_replace = os.replace
        checks = 0
        def injected_health(workspace):
            global checks
            checks += 1
            if fault == "health" and checks == 2:
                raise WorkspaceError("Injected private health failure.")
            original_health(workspace)
        def injected_replace(source, destination, *args, **kwargs):
            if Path(destination).name == "current.json":
                phase = json.loads(Path(source).read_bytes())["state"]
                if phase == "selected" and fault == "before":
                    os._exit(72)
                original_replace(source, destination, *args, **kwargs)
                if phase == "selected" and fault == "after":
                    os._exit(73)
            else:
                original_replace(source, destination, *args, **kwargs)
        check_health = injected_health
        os.replace = injected_replace

if __name__ == "__main__":"""))
        forward = self.fixture_commit("Activation fault fixture")
        workspace = self.root / "old workspace"
        database = database_path(workspace)
        workspace.mkdir(mode=0o700)
        database.parent.mkdir(mode=0o700)
        (workspace / ".maintenance.lock").touch(mode=0o600)
        initialize_database(database, "Synthetic shop", "UTC", "owner", "unused", demo=True)
        candidate = self.root / "staged installation"
        clone = candidate / "workspace"
        launcher = self.target / "repairlib"

        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, self.target)
            self.installer.stage_update(script, forward, self.target, workspace, candidate)
        backup = candidate / "backup.zip"
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        original = database.read_bytes()
        old_install = (self.target / "install.json").read_bytes()
        selection = self.target / "current.json"

        def run_launcher(launcher, *args):
            return subprocess.run([launcher, *args], capture_output=True, text=True, timeout=15)

        activate = self.installer.activate_update
        recover = self.installer.recover_update
        with self.installer.installation_lock(self.target):
            with self.assertRaisesRegex(self.installer.InstallError, "installation lock"):
                activate(script, self.target, candidate)
        for locked in (workspace, clone):
            with maintenance_lock(locked, exclusive=False, create=False):
                with self.assertRaisesRegex(self.installer.InstallError, "app is running"):
                    activate(script, self.target, candidate)
            self.assertFalse(selection.exists())

        for changed in (database, database_path(clone), backup,
                        candidate / "generation/source/README.md"):
            previous = changed.read_bytes()
            try:
                changed.write_bytes(previous + b"changed")
                with self.assertRaisesRegex(self.installer.InstallError, "changed"):
                    activate(script, self.target, candidate)
                self.assertFalse(selection.exists())
            finally:
                changed.write_bytes(previous)

        stable = launcher.read_bytes()
        for fault, phase in (("health", None), ("before", "pending"), ("after", "selected"), ("", "ready")):
            with self.subTest(fault=fault or "success"):
                with patch.dict(os.environ, {"REPAIRLIB_FIXTURE_FAULT": fault}):
                    if fault:
                        with self.assertRaisesRegex(self.installer.InstallError, "health|interrupted"):
                            activate(script, self.target, candidate)
                    else:
                        activated = subprocess.run(
                            [sys.executable, "-I", "-B", script, "--activate-update",
                             "--target", self.target, "--candidate", candidate],
                            capture_output=True, text=True, timeout=30,
                        )
                        self.assertEqual(activated.returncode, 0, activated.stderr)
                        self.assertEqual(json.loads(activated.stdout)["state"], "ready")
                if phase is None:
                    self.assertFalse(selection.exists())
                else:
                    self.assertEqual(json.loads(selection.read_bytes())["state"], phase)
                    self.assertNotEqual(run_launcher(launcher, "dev").returncode, 0)
                    self.assertNotEqual(run_launcher(candidate / "repairlib", "dev").returncode, 0)
                    if phase == "ready":
                        status = run_launcher(launcher, "status", "--json")
                        self.assertEqual(status.returncode, 0, status.stderr)
                        readback = json.loads(status.stdout)
                        self.assertEqual(readback["workspace"], str(clone))
                        self.assertEqual(readback["customers"], 3)

                    if phase == "selected":
                        clone_db = database_path(clone)
                        before = clone_db.read_bytes()
                        with closing(sqlite3.connect(clone_db)) as db, db:
                            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Newer state')")
                        newer = clone_db.read_bytes()
                        self.assertNotEqual(newer, before)
                        with self.assertRaisesRegex(self.installer.InstallError, "changed"):
                            recover(script, self.target, candidate)
                        self.assertEqual(clone_db.read_bytes(), newer)
                        self.assertEqual(json.loads(selection.read_bytes())["state"], phase)
                        clone_db.write_bytes(before)

                    recovered = subprocess.run(
                        [sys.executable, "-I", "-B", script, "--recover-update",
                         "--target", self.target, "--candidate", candidate],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertEqual(json.loads(recovered.stdout)["state"], "original")
                    self.assertFalse(selection.exists())
                self.assertEqual(launcher.read_bytes(), stable)
                old = run_launcher(launcher, "status", "--json", "--workspace", workspace)
                self.assertEqual(old.returncode, 0, old.stderr)
                self.assertEqual(json.loads(old.stdout)["workspace"], str(workspace))
                self.assertEqual(database.read_bytes(), original)
                self.assertEqual((self.target / "install.json").read_bytes(), old_install)
                self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), backup_hash)
        self.assertEqual(verify_backup(backup), ("Backup archive", SCHEMA_VERSION))

    def test_listener_write_boundary_and_post_update_recovery(self):
        script = self.repo / "scripts/install.py"
        for name in ("__main__.py", "app.py", "cli/workspace.py", "lifecycle/operations.py",
                     "lifecycle/service.py", "lifecycle/update.py"):
            shutil.copyfile(ROOT / "src/repairlib" / name, self.repo / "src/repairlib" / name)
        update = self.repo / "src/repairlib/lifecycle/update.py"
        update.write_text(update.read_text().replace('if __name__ == "__main__":', """
if len(sys.argv) > 1 and sys.argv[1] == "reopen":
    fault = os.environ.get("REPAIRLIB_FIXTURE_REOPEN_EXIT")
    original_replace = os.replace
    def injected_replace(source, destination, *args, **kwargs):
        opening = (Path(destination).name == "current.json"
                   and json.loads(Path(source).read_bytes())["state"] == "open")
        if opening and fault == "before":
            os._exit(74)
        original_replace(source, destination, *args, **kwargs)
        if opening and fault == "after":
            os._exit(75)
    os.replace = injected_replace

if __name__ == "__main__":"""))
        forward = self.fixture_commit("Disposable listener write boundary")
        old = self.root / "original workspace"
        candidate = self.root / "staged update"
        launcher = self.target / "repairlib"
        descriptor = self.target / "current.json"
        password = "synthetic update owner password"
        initialize_demo_workspace(old, username="owner", password=password)
        now = int(time())
        session = sessions.login(old, "owner", password, now=now)
        token = tokens.create_token(old, session, password=password, name="Update fixture",
                                    scope="write", key="a" * 32, now=now)

        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, self.target)
            self.installer.stage_update(script, forward, self.target, old, candidate)
        old_bytes = database_path(old).read_bytes()
        with socket.socket() as picker:
            picker.bind(("127.0.0.1", 0))
            port = picker.getsockname()[1]

        def install_command(mode, *args, fault=""):
            return subprocess.run(
                [sys.executable, "-I", "-B", script, mode, "--target", self.target,
                 "--candidate", candidate, *args], capture_output=True, text=True, timeout=30,
                env=os.environ | {"REPAIRLIB_FIXTURE_REOPEN_EXIT": fault},
            )

        def phase():
            return json.loads(descriptor.read_bytes())["state"]

        def request(path, *, key=None, data=None, content_type="application/json"):
            headers = {"Authorization": f"Bearer {token}"}
            if key is not None:
                headers.update({"Idempotency-Key": key, "Content-Type": content_type})
            if isinstance(data, dict):
                data = json.dumps(data).encode()
            req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
            try:
                with urlopen(req, timeout=2) as response:
                    return response.status, response.read()
            except HTTPError as error:
                with error:
                    return error.code, error.read()

        @contextmanager
        def supervise(executable):
            # A disposable supervisor hands off the real loopback foreground listener.
            env = os.environ.copy()
            env.pop("NOTIFY_SOCKET", None)
            with subprocess.Popen([executable, "service", "foreground", "--port", str(port)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                try:
                    deadline = monotonic() + 10
                    while True:
                        try:
                            request("/health")
                            break
                        except OSError:
                            self.assertIsNone(process.poll(), "foreground listener exited before health")
                            self.assertLess(monotonic(), deadline, "foreground listener did not bind")
                            sleep(0.05)
                    yield
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

        def status():
            result = subprocess.run([launcher, "status", "--json"], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        customers = "/api/v1/customers"
        reopen = ("--reopen-update", "--port", str(port))
        activated = install_command("--activate-update")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(phase(), "ready")
        doctor = subprocess.run([launcher, "doctor"], capture_output=True, text=True, timeout=10)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn("Update phase: ready (writes: closed)", doctor.stdout)

        class OtherListener(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ready": true, "update_phase": "reopening", "writes_open": false}')

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), OtherListener) as unrelated:
            probe = Thread(target=unrelated.handle_request)
            unrelated.timeout = 10
            probe.start()
            try:
                wrong_listener = install_command("--reopen-update", "--port", str(unrelated.server_port))
                self.assertNotEqual(wrong_listener.returncode, 0)
                self.assertIn("could not verify the selected candidate listener", wrong_listener.stderr.lower())
                self.assertEqual(phase(), "reopening")
            finally:
                probe.join()
        with supervise(launcher):
            self.assertEqual(status()["writes_open"], False)
            self.assertEqual(request(customers)[0], 503)
            self.assertEqual(request(customers, key="blocked", data={"name": "Blocked"})[0], 503)
            interrupted = install_command(*reopen, fault="before")
            self.assertIn("interrupted", interrupted.stderr)
            self.assertEqual(phase(), "reopening")
            self.assertEqual(request(customers, key="blocked", data={"name": "Blocked"})[0], 503)
            self.assertEqual(status()["customers"], 3)
        recovered = install_command("--recover-update")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["state"], "original")
        self.assertFalse(descriptor.exists())

        activated = install_command("--activate-update")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        with supervise(launcher):
            reopened = install_command(*reopen)
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            self.assertEqual(phase(), "open")
            self.assertEqual(status()["writes_open"], True)
            # A completed update leaves ordinary launcher commands usable on the selected workspace.
            listed = subprocess.run([launcher, "customers", "list"], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("Synthetic Customer One", listed.stdout)
        # An exit after publishing the boundary must make exactly the same
        # promise as a successful reopen. Use a second disposable selection.
        other_target = self.root / "second installation"
        other_candidate = self.root / "second candidate"
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, other_target)
            self.installer.stage_update(script, forward, other_target, old, other_candidate)
        self.target, candidate = other_target, other_candidate
        launcher, descriptor = self.target / "repairlib", self.target / "current.json"
        clone = candidate / "workspace"
        backup_hash = hashlib.sha256((candidate / "backup.zip").read_bytes()).hexdigest()
        activated = install_command("--activate-update")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        original = b"committed original\x00\xff"
        with supervise(launcher):
            interrupted = install_command(*reopen, fault="after")
            self.assertIn("interrupted", interrupted.stderr)
            self.assertEqual(phase(), "open")
            code, payload = request(customers, key="written", data={"name": "Newer candidate"})
            self.assertEqual(code, 201)
            customer = json.loads(payload)["id"]
            code, payload = request("/api/v1/repairs", key="repair",
                                    data={"customer_id": customer, "reported_issue": "Synthetic repair"})
            self.assertEqual(code, 201)
            repair = json.loads(payload)["id"]
            code, payload = request(f"/api/v1/repairs/{repair}/attachments?filename=original.bin",
                                    key="attachment", data=original, content_type="application/octet-stream")
            self.assertEqual(code, 201)
            attachment = json.loads(payload)["id"]
        content = f"/api/v1/repairs/{repair}/attachments/{attachment}/content"
        recovered = install_command("--recover-update")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["state"], "open")
        self.assertEqual(json.loads(recovered.stdout)["attachments_checked"], 1)
        self.assertEqual(status()["customers"], 4)
        self.assertEqual(database_path(old).read_bytes(), old_bytes)
        self.assertTrue((self.target / "generation/source").is_dir())
        self.assertEqual(hashlib.sha256((candidate / "backup.zip").read_bytes()).hexdigest(), backup_hash)
        third_commit = self.fixture_commit("Next update after newer writes")
        third_candidate = self.root / "third candidate"
        selected_bytes = descriptor.read_bytes()
        preflight = self.installer.check_update(script, third_commit, self.target)
        self.assertEqual((preflight["selected_commit"], preflight["blocker"]), (forward, None))
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            with self.assertRaisesRegex(self.installer.InstallError, "selected workspace"):
                self.installer.stage_update(script, third_commit, self.target, old, third_candidate)
            self.assertFalse(third_candidate.exists())
            staged = self.installer.stage_update(script, third_commit, self.target, clone, third_candidate)
        self.assertEqual(staged["source_commit"], forward)
        self.assertEqual(descriptor.read_bytes(), selected_bytes)
        second_candidate, candidate = candidate, third_candidate
        activated = install_command("--activate-update")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        with supervise(launcher):
            interrupted = install_command(*reopen, fault="before")
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertEqual(phase(), "reopening")
        rolled_back = install_command("--recover-update")
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertEqual(descriptor.read_bytes(), selected_bytes)
        candidate = second_candidate
        with supervise(launcher):
            self.assertEqual(json.loads(request(f"{customers}/{customer}")[1])["name"], "Newer candidate")
            self.assertEqual(request(content), (200, original))
        blob, = (database_path(clone).parent / "files" / "shop-1").iterdir()
        self.assertEqual(blob.read_bytes(), original)
        # A stopped candidate with damaged originals requires reconciliation.
        blob.write_bytes(b"synthetic damage")
        refused = install_command("--recover-update")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("reconciliation", refused.stderr.lower())
        self.assertEqual(phase(), "open")
        self.assertEqual(blob.read_bytes(), b"synthetic damage")
        self.assertEqual(database_path(old).read_bytes(), old_bytes)
        blob.write_bytes(original)
        # Even if a direct out-of-band writer advances the retained source,
        # recovery must preserve both copies and explicitly require reconciliation.
        with closing(sqlite3.connect(database_path(old))) as db, db:
            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Original-side write')")
        divergent = database_path(old).read_bytes()
        self.assertNotEqual(divergent, old_bytes)
        refused = install_command("--recover-update")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("reconciliation", refused.stderr.lower())
        self.assertEqual(phase(), "open")
        self.assertEqual(database_path(old).read_bytes(), divergent)
        with supervise(launcher):
            self.assertEqual(request(content), (200, original))

        candidate = self.root / "retry third candidate"
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.stage_update(script, third_commit, self.target, clone, candidate)
        activated = install_command("--activate-update")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        with supervise(launcher):
            interrupted = install_command(*reopen, fault="after")
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertEqual(phase(), "open")
            code, payload = request(customers, key="written-on-c", data={"name": "Newer third generation"})
            self.assertEqual(code, 201)
            third_customer = json.loads(payload)["id"]
        recovered = install_command("--recover-update")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["commit"], third_commit)
        with supervise(launcher):
            self.assertEqual(json.loads(request(f"{customers}/{third_customer}")[1])["name"], "Newer third generation")
            self.assertEqual(json.loads(request(f"{customers}/{customer}")[1])["name"], "Newer candidate")
            self.assertEqual(request(content), (200, original))

    def test_managed_fixed_path_handoff(self):
        script = self.repo / "scripts/install.py"
        for name in ("lifecycle/service.py", "lifecycle/update.py"):
            shutil.copyfile(ROOT / "src/repairlib" / name, self.repo / "src/repairlib" / name)
        update = self.repo / "src/repairlib/lifecycle/update.py"
        update.write_text(update.read_text().replace('if __name__ == "__main__":', """
replace = os.replace
def interrupt(source, destination, *args, **kwargs):
    if Path(destination).name == "current.json":
        phase = json.loads(Path(source).read_bytes())["state"]
        fault = os.environ.get("REPAIRLIB_FIXTURE_MANAGED_EXIT")
        if (phase, fault) in {("pending", "before"), ("open", "closed")}:
            os._exit(76)
        replace(source, destination, *args, **kwargs)
        if (phase, fault) in {("selected", "after"), ("open", "open")}:
            os._exit(77)
    else:
        replace(source, destination, *args, **kwargs)
os.replace = interrupt
if __name__ == "__main__":"""))
        forward = self.fixture_commit("Managed handoff fixture")
        workspace = self.root / "original workspace"
        candidate = self.root / "candidate"
        launcher = self.target / "repairlib"
        descriptor = self.target / "current.json"
        password = "synthetic managed owner password"
        initialize_demo_workspace(workspace, username="owner", password=password)
        now = int(time())
        session = sessions.login(workspace, "owner", password, now=now)
        token = tokens.create_token(workspace, session, password=password, name="Managed fixture",
                                    scope="write", key="b" * 32, now=now)

        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            self.installer.install(script, self.commit, self.target)
            self.installer.stage_update(script, forward, self.target, workspace, candidate)
        original = database_path(workspace).read_bytes()
        backup_hash = hashlib.sha256((candidate / "backup.zip").read_bytes()).hexdigest()

        with socket.socket() as picker:
            picker.bind(("127.0.0.1", 0))
            port = str(picker.getsockname()[1])
        config = self.root / "config"
        unit = config / "systemd/user/repairlib.service"
        runner = self.root / "systemctl"
        address = str(self.root / "ctl.sock")
        runner.write_text(f"#!{sys.executable}\n" + """
import json, socket, sys
with socket.socket(socket.AF_UNIX) as client:
    client.connect(sys.argv[0].rsplit("/", 1)[0] + "/ctl.sock")
    client.sendall(json.dumps(sys.argv[1:]).encode() + b"\\n")
    with client.makefile() as response:
        code, output = json.load(response)
print(output, end="")
sys.exit(code)
""")
        runner.chmod(0o700)
        env = os.environ | {"XDG_CONFIG_HOME": str(config), "PATH": f"{self.root}:{os.environ['PATH']}"}
        process = None
        loaded = None
        failure = ""
        drop_ins = ""
        reported_pid = None
        actions = []

        def stop():
            nonlocal process
            if process is not None:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process = None

        self.addCleanup(stop)

        def supervisor(argv):
            nonlocal process, loaded
            self.assertEqual(argv[0], "--user")
            action = argv[1]
            if action == "show":
                pid = reported_pid or (process.pid if process and process.poll() is None else 0)
                values = {"LoadState": "loaded" if loaded else "not-found",
                          "ActiveState": "active" if pid else "inactive",
                          "SubState": "running" if pid else "dead", "MainPID": pid,
                          "FragmentPath": str(unit) if loaded else "", "DropInPaths": drop_ins,
                          "NeedDaemonReload": "yes" if loaded and loaded != unit.read_bytes() else "no"}
                return (0 if loaded else 4), "".join(f"{key}={value}\n" for key, value in values.items())
            actions.append(action)
            if failure == action:
                return 1, ""
            if action == "daemon-reload":
                loaded = unit.read_bytes()
            elif action == "stop":
                stop()
            elif action in ("start", "restart"):
                stop()
                command = next(line for line in loaded.decode().splitlines() if line.startswith("ExecStart=:"))
                argv = [arg.replace("%%", "%") for arg in shlex.split(command.removeprefix("ExecStart=:"))]
                notify_address = str(self.root / "ready.sock")
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                    Path(notify_address).unlink(missing_ok=True)
                    notify.bind(notify_address)
                    notify.settimeout(0.1)
                    process = subprocess.Popen(argv, env=env | {"NOTIFY_SOCKET": notify_address},
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    deadline = monotonic() + 10
                    while monotonic() < deadline and process.poll() is None:
                        try:
                            self.assertEqual(notify.recv(128), b"READY=1")
                            return 0, ""
                        except TimeoutError:
                            pass
                    stop()
                    return 1, ""
            else:
                self.fail(f"Unexpected supervisor action {action}")
            return 0, ""

        class Runner(StreamRequestHandler):
            def handle(self):
                result = supervisor(json.loads(self.rfile.readline()))
                self.wfile.write(json.dumps(result).encode())

        def run(argv):
            return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

        def command(action, *, fault="", success=True, message="", listener_port=port):
            env["REPAIRLIB_FIXTURE_MANAGED_EXIT"] = fault
            result = run([sys.executable, "-I", "-B", script, f"--{action}-update",
                          "--managed", "--port", listener_port,
                          "--target", self.target, "--candidate", candidate])
            self.assertEqual(result.returncode == 0, success, result.stderr)
            if success:
                return json.loads(result.stdout)["state"]
            self.assertIn(message, result.stderr.lower())

        def phase():
            return json.loads(descriptor.read_bytes())["state"] if descriptor.exists() else None

        def request(path, *, name=None):
            headers = {"Authorization": f"Bearer {token}"}
            data = None
            if name:
                headers.update({"Idempotency-Key": "managed-newer", "Content-Type": "application/json"})
                data = json.dumps({"name": name}).encode()
            try:
                with urlopen(Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers),
                             timeout=2) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        with UnixStreamServer(address, Runner) as manager:
            thread = Thread(target=manager.serve_forever)
            thread.start()
            try:
                args = ["--workspace", workspace, "--port", port]
                for action in ("install", "start"):
                    result = run([launcher, "service", action, *args])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                fixed = unit.read_bytes()
                self.assertIn(str(self.installer.SYSTEM_PYTHON).encode(), fixed)
                self.assertIn(b"/generation/source/src", fixed)
                actions.clear()

                drop_ins = "/foreign/override.conf"
                command("activate", success=False, message="unmanaged")
                drop_ins = ""
                reported_pid = os.getpid()
                command("activate", success=False, message="unrelated")
                reported_pid = None
                self.assertEqual(actions, [])
                self.assertEqual(unit.read_bytes(), fixed)

                failure = "stop"
                command("activate", success=False, message="stop failed")
                self.assertIsNone(phase())
                self.assertEqual(unit.read_bytes(), fixed)
                self.assertEqual(request("/health")[0], 200)
                failure = ""

                stop()
                with HTTPServer(("127.0.0.1", int(port)), BaseHTTPRequestHandler):
                    actions.clear()
                    command("activate", success=False, message="unrelated listener")
                    self.assertIsNone(phase())
                    self.assertEqual(actions, [])
                self.assertEqual(command("recover"), "original")

                failure = "daemon-reload"
                command("activate", success=False, message="daemon-reload failed")
                self.assertIsNone(phase())
                self.assertIsNone(process)
                stable = unit.read_bytes()
                self.assertNotEqual(stable, fixed)
                self.assertNotIn(b"/generation/", stable)
                self.assertNotIn(os.fsencode(candidate), stable)
                failure = ""
                self.assertEqual(command("recover"), "original")
                self.assertTrue(request("/health")[1]["ready"])

                for fault, expected in (("before", None), ("after", "selected")):
                    command("activate", fault=fault, success=False, message="interrupted")
                    self.assertEqual(phase(), expected)
                    self.assertIsNone(process)
                    self.assertEqual(command("recover"), "original")
                    self.assertIsNone(phase())
                    self.assertEqual(unit.read_bytes(), stable)
                    self.assertTrue(request("/health")[1]["ready"])

                failure = "start"
                command("activate", success=False, message="start failed")
                self.assertEqual(phase(), "ready")
                self.assertIsNone(process)
                failure = ""
                command("reopen", fault="closed", success=False, message="interrupted")
                self.assertEqual(phase(), "reopening")
                self.assertEqual(request("/api/v1/customers", name="Blocked")[0], 503)
                self.assertEqual(command("recover"), "original")
                actions.clear()
                self.assertEqual(command("activate"), "ready")
                self.assertEqual(actions, ["stop", "start"])
                self.assertEqual(unit.read_bytes(), stable)
                self.assertEqual(request("/health")[1]["writes_open"], False)
                self.assertEqual(request("/api/v1/customers", name="Blocked")[0], 503)
                wrong_port = "8001" if port == "8000" else "8000"
                actions.clear()
                command("reopen", listener_port=wrong_port, success=False, message="unit and port")
                self.assertEqual(actions, [])
                self.assertEqual(phase(), "ready")

                command("reopen", fault="open", success=False, message="interrupted")
                self.assertEqual(phase(), "open")
                code, payload = request("/api/v1/customers", name="Managed newer")
                self.assertEqual(code, 201)
                customer = payload["id"]
                prior_pid = process.pid
                self.assertEqual(command("recover"), "open")
                self.assertNotEqual(process.pid, prior_pid)
                self.assertEqual(unit.read_bytes(), stable)
                self.assertEqual(request(f"/api/v1/customers/{customer}")[1]["name"], "Managed newer")
                self.assertEqual(database_path(workspace).read_bytes(), original)
                self.assertEqual(hashlib.sha256((candidate / "backup.zip").read_bytes()).hexdigest(),
                                 backup_hash)
            finally:
                stop()
                manager.shutdown()
                thread.join()

    def test_stages_one_commit_with_the_stock_interpreter(self):
        (self.repo / "untracked").touch()
        real_git = self.installer.git
        with (patch.object(self.installer, "git", wraps=real_git) as git,
              patch.object(self.installer, "provision") as provision):
            report = self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target)

        self.assertEqual(report["commit"], self.commit)
        self.assertEqual(sum(call.args[1] == "rev-parse" for call in git.call_args_list), 1)
        project = self.target / "generation/source"
        self.assertFalse((project / "untracked").exists())
        generation = project.parent
        provision.assert_called_once_with(generation)
        self.assertEqual(json.loads((self.target / "install.json").read_text()), report)
        self.assertFalse(self.target.stat().st_mode & 0o077)
        launcher = shlex.split((self.target / "repairlib").read_text().split("\n", 1)[1])
        self.assertEqual(launcher[1], str(self.installer.SYSTEM_PYTHON))
        self.assertEqual(launcher[-3:], [str(self.target), "", "$@"])
        self.assertFalse((generation / "venv").exists() or (generation / "uv").exists())

    def test_install_links_the_command_and_the_skill_into_home(self):
        skills = self.repo / "skills/repairlib"
        if not (skills / "SKILL.md").is_file():
            skills.mkdir(parents=True)
            (skills / "SKILL.md").write_text("---\nname: repairlib\n---\n")
            self.fixture_commit("Skill fixture")
        home = self.root / "home"
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            report = self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target, home=home)

        command = home / ".local/bin/repairlib"
        skill = home / ".agents/skills/repairlib"
        self.assertEqual([link["status"] for link in report["links"]], ["created", "created"])
        self.assertEqual(command.resolve(), (self.target / "repairlib").resolve())
        self.assertEqual(skill.resolve(), (self.repo / "skills/repairlib").resolve())
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertEqual(json.loads((self.target / "install.json").read_text()), report)

    def test_an_unwritable_home_link_is_reported_and_keeps_the_installation(self):
        home = self.root / "home"
        (home / ".local").mkdir(parents=True)
        (home / ".local" / "bin").write_text("not a directory")
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            report = self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target, home=home)
        self.assertEqual(report["links"][0]["status"], "blocked")
        self.assertIn("reason", report["links"][0])
        self.assertTrue((self.target / "repairlib").is_file())
        self.assertTrue((self.target / "generation/source/skills/repairlib/SKILL.md").is_file())

    def test_install_without_a_home_writes_no_links(self):
        with patch.object(self.installer, "provision", side_effect=self.local_runtime):
            report = self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target)
        self.assertNotIn("links", report)
        self.assertEqual(json.loads((self.target / "install.json").read_text()), report)

    def test_provision_runs_the_staged_source_on_distribution_packages_without_network(self):
        generation = self.root / "stock generation"
        (generation / "source").parent.mkdir()
        shutil.copytree(ROOT / "src", generation / "source/src", ignore=shutil.ignore_patterns("__pycache__"))
        with patch("urllib.request.urlopen", side_effect=AssertionError("no downloads")):
            self.installer.provision(generation)

    def test_provision_names_missing_distribution_packages(self):
        generation = self.root / "missing generation"
        generation.mkdir()
        with patch.object(self.installer, "REQUIRED_MODULES", ("json", "no_such_distribution_module")):
            with self.assertRaisesRegex(self.installer.InstallError, "no_such_distribution_module"):
                self.installer.provision(generation)

    def test_host_plan_reports_the_system_interpreter(self):
        plan = self.installer.host_plan()
        self.assertEqual(plan["python"], {"mode": "system", "version": platform.python_version(),
                                          "executable": str(self.installer.SYSTEM_PYTHON)})
        for machine in ("x86_64", "aarch64"):
            with patch.object(self.installer.platform, "machine", return_value=machine):
                self.installer.host_plan()

    def test_refuses_unsupported_architecture_old_python_and_foreign_interpreter(self):
        for machine in ("armv7l", "i686", ""):
            with self.subTest(machine=machine):
                with patch.object(self.installer.platform, "machine", return_value=machine):
                    with self.assertRaisesRegex(self.installer.InstallError, "Unsupported architecture"):
                        self.installer.host_plan()
        with patch.object(self.installer.sys, "version_info", (3, 12, 9)):
            with self.assertRaisesRegex(self.installer.InstallError, "3.13 or newer"):
                self.installer.host_plan()
        with patch.object(self.installer.sys, "executable", "/opt/private/bin/python3"):
            with self.assertRaisesRegex(self.installer.InstallError, "distribution's"):
                self.installer.host_plan()

    def test_installer_and_service_agree_on_the_interpreter_path(self):
        from repairlib.lifecycle.update import ManagedListener

        generation = self.root / "agree"
        argv = ManagedListener.runtime_argv(generation.parent, "boot")
        self.assertEqual(argv[0], str(self.installer.SYSTEM_PYTHON))

    def test_update_refuses_when_openrc_service_is_registered(self):
        service = self.root / "repairlib"
        service.write_text("managed")
        with patch.object(self.installer, "OPENRC_SERVICE", service):
            with self.assertRaisesRegex(self.installer.InstallError, "OpenRC repairlib service"):
                self.installer.stage_update(
                    self.repo / "scripts/install.py", self.commit,
                    self.target, self.root / "workspace", self.root / "candidate")

    def test_refuses_unsafe_targets_and_discards_failed_install(self):
        existing = self.root / "existing"
        existing.write_text("keep")
        linked = self.root / "linked"
        linked.symlink_to(existing)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for target in (existing, linked, alias / "nested", self.root / ".." / "escape",
                       self.repo / "nested", self.root / "line\nbreak", Path("relative")):
            with self.subTest(target=target), patch.object(self.installer, "provision") as provision:
                with self.assertRaises(self.installer.InstallError):
                    self.installer.install(self.repo / "scripts/install.py", "HEAD", target)
                provision.assert_not_called()
        self.assertEqual(existing.read_text(), "keep")
        self.assertTrue(linked.is_symlink())

        (self.repo / "scripts/install.py").write_text("different installer")
        with self.assertRaisesRegex(self.installer.InstallError, "match"):
            self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target)
        self.assertFalse(self.target.exists())
        shutil.copyfile(SCRIPT, self.repo / "scripts/install.py")
        with patch.object(self.installer, "provision", side_effect=self.installer.InstallError("failed")):
            with self.assertRaisesRegex(self.installer.InstallError, "failed"):
                self.installer.install(self.repo / "scripts/install.py", "HEAD", self.target)
        self.assertFalse(self.target.exists())
