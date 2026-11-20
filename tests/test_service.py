"""Linux user unit contracts with an isolated supervisor and workspace."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from time import time
from urllib.request import urlopen
from unittest.mock import patch

from repairlib.__main__ import main
from repairlib.domain import sessions, tokens
from repairlib.domain.setup import initialize_demo_workspace
from repairlib.lifecycle.service import BOOT, SOURCE, find_process, foreground_args, ignored_in_checkout, python_args
from repairlib.lifecycle.workspace import WorkspaceError, database_path, maintenance_lock
from repairlib.storage.initialization import initialize_database
from test_baseline import python_env
from test_cli_identity import COMMAND, running_listener
from test_demo import PASSWORD


class ServiceManagerTests(unittest.TestCase):
    def test_openrc_is_detected_only_without_a_running_systemd(self):
        from repairlib.lifecycle import service

        with patch.object(service.Path, "is_dir", lambda self: False), \
                patch.object(service.Path, "is_file", lambda self: True), \
                patch.object(service, "which", lambda name: "/sbin/rc-service"):
            self.assertTrue(service.using_openrc())
        with patch.object(service.Path, "is_dir", lambda self: True):
            self.assertFalse(service.using_openrc())


class ServicePathTests(unittest.TestCase):
    def test_a_git_ignored_checkout_workspace_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            subprocess.run(["git", "-C", temp, "init", "-q"], check=True)
            (checkout / ".gitignore").write_text("/data/\n", encoding="utf8")
            (checkout / "data").mkdir()
            self.assertTrue(ignored_in_checkout(checkout / "data", checkout))
            self.assertFalse(ignored_in_checkout(checkout / "other", checkout))


class ServiceHelpTests(unittest.TestCase):
    def test_top_level_help_names_both_supported_supervisors(self):
        from repairlib.__main__ import build_parser

        help_text = build_parser().format_help()
        self.assertIn("OpenRC", help_text)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0, "unprivileged Linux user service")
class UserServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'shop "quoted" %n $HOME \\ files'
        self.config = self.root / "config"
        self.unit = self.config / "systemd/user/repairlib.service"
        self.fragment = ""
        self.active = "inactive"
        self.drop_ins = ""
        self.offline = False
        self.fail_reload = False
        self.start_without_process = False
        self.listener_ok = True
        self.health_ok = True
        self.listener_calls = []
        self.health_calls = []
        self.calls = []

    def supervisor(self, argv, **kwargs):
        self.assertEqual(Path(argv[0]).name, "systemctl")
        self.assertEqual(argv[1], "--user")
        action = argv[2]
        self.calls.append(action)
        if self.offline or (action == "daemon-reload" and self.fail_reload):
            return subprocess.CompletedProcess(argv, 1, "", "manager unavailable")
        if action == "daemon-reload":
            self.fragment = str(self.unit) if self.unit.is_file() else ""
        elif action in ("start", "restart"):
            self.active = "inactive" if self.start_without_process else "active"
        elif action == "stop":
            self.active = "inactive"
        elif action == "show":
            running = self.active == "active"
            properties = {
                "LoadState": "loaded" if self.fragment else "not-found",
                "ActiveState": self.active,
                "SubState": "running" if running else "dead",
                "MainPID": "123" if running else "0",
                "FragmentPath": self.fragment,
                "DropInPaths": self.drop_ins,
                "NeedDaemonReload": "no",
            }
            output = "".join(f"{key}={value}\n" for key, value in properties.items())
            return subprocess.CompletedProcess(argv, 0 if self.fragment else 4, output, "")
        else:
            self.fail(f"Unexpected supervisor command {action}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def service(self, action, *, workspace=None):
        output, errors = io.StringIO(), io.StringIO()
        args = ["service", action, "--workspace", str(workspace or self.workspace)]
        module = "repairlib.lifecycle.service."

        def owns_listener(pid, port):
            self.listener_calls.append((pid, port))
            return self.listener_ok

        def health_ready(port):
            self.health_calls.append(port)
            return self.health_ok

        with (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}),
              patch("subprocess.run", side_effect=self.supervisor),
              patch(module + "owns_listener", owns_listener),
              patch(module + "health_ready", health_ready),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            try:
                code = main(args)
            except SystemExit as error:
                code = error.code
        self.last_output = output.getvalue() + errors.getvalue()
        return code, json.loads(output.getvalue()) if output.getvalue() else {}

    def demo(self):
        initialize_demo_workspace(self.workspace, username="owner", password=PASSWORD)

    def test_register_setup_lifecycle_and_preserve_populated_workspace(self):
        code, report = self.service("install")
        self.assertEqual(code, 0, self.last_output)
        self.assertEqual((report["service"], report["workspace_state"]), ("stopped", "missing"))
        self.assertIn("setup", report["next"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.unit.stat().st_mode & 0o777, 0o600)
        control = self.config / "repairlib-service"
        self.assertEqual(control.stat().st_mode & 0o777, 0o700)
        unit = self.unit.read_text()
        self.assertIn('shop \\"quoted\\" %%n $HOME \\\\ files', unit)
        self.assertIn("ExecStart=:", unit)
        self.assertIn("Type=notify", unit)
        self.assertNotEqual(self.service("start")[0], 0)
        self.assertNotIn("start", self.calls)
        self.demo()
        before = database_path(self.workspace).read_bytes()

        for action, expected in (("start", "running"), ("status", "running"),
                                 ("restart", "running"), ("stop", "stopped"),
                                 ("start", "running"), ("remove", "absent"),
                                 ("status", "absent")):
            self.listener_calls.clear()
            self.health_calls.clear()
            code, report = self.service(action)
            self.assertEqual(code, 0, self.last_output)
            self.assertEqual(report["service"], expected, action)
            self.assertEqual(report["workspace_state"], "initialized")
            if expected == "running":
                self.assertEqual(self.listener_calls, [(123, 8000)], action)
                self.assertEqual(self.health_calls, [8000], action)
                self.assertEqual(report["health"], "ready", action)
            else:
                self.assertEqual(self.listener_calls, [], action)
                self.assertEqual(self.health_calls, [], action)
                self.assertIsNone(report["health"], action)
        self.assertFalse(self.unit.exists())
        self.assertEqual(database_path(self.workspace).read_bytes(), before)
        self.assertTrue((control / ".maintenance.lock").is_file())

    def test_active_process_without_readiness_is_unready(self):
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.demo()
        # Start healthy first so the fake supervisor has an active unit with a MainPID.
        code, report = self.service("start")
        self.assertEqual(code, 0, self.last_output)
        self.assertEqual(report["service"], "running")

        for listener_ok, health_ok, failing in (
                (False, False, {"listener", "health"}),
                (True, False, {"health"}),
                (False, True, {"listener"})):
            with self.subTest(listener=listener_ok, health=health_ok):
                self.listener_ok = listener_ok
                self.health_ok = health_ok

                self.listener_calls.clear()
                self.health_calls.clear()
                code, report = self.service("status")
                self.assertEqual(code, 0, self.last_output)
                self.assertEqual((report["service"], report["health"]), ("unready", "unavailable"))
                self.assertEqual((report["active_state"], report["substate"], report["pid"]),
                                 ("active", "running", 123))
                for name in ("listener", "health"):
                    if name in failing:
                        self.assertIn(name, report["error"])
                    else:
                        self.assertNotIn(name, report["error"])
                self.assertEqual(self.listener_calls, [(123, 8000)])
                self.assertEqual(self.health_calls, [8000])

                for action in ("start", "restart"):
                    self.listener_calls.clear()
                    self.health_calls.clear()
                    code, report = self.service(action)
                    self.assertNotEqual(code, 0, action)
                    self.assertEqual((report["service"], report["health"]),
                                     ("unready", "unavailable"), action)
                    self.assertNotEqual(report["service"], "running", action)
                    self.assertIn("readiness", report["error"], action)
                    self.assertEqual(self.listener_calls, [(123, 8000)], action)
                    self.assertEqual(self.health_calls, [8000], action)

    def test_install_creates_private_config_tree_under_permissive_umask(self):
        self.assertFalse(self.config.exists())
        self.assertFalse((self.config / "systemd").exists())
        original_umask = os.umask(0o002)
        try:
            code, _ = self.service("install")
            self.assertEqual(code, 0, self.last_output)
        finally:
            os.umask(original_umask)
        for directory in (self.config, self.config / "systemd",
                          self.config / "systemd/user", self.config / "repairlib-service"):
            info = directory.lstat()
            self.assertTrue(stat.S_ISDIR(info.st_mode), directory)
            self.assertEqual(info.st_mode & 0o022, 0, directory)
            self.assertEqual(info.st_mode & 0o777, 0o700, directory)
        code, _ = self.service("status")
        self.assertEqual(code, 0, self.last_output)

    def test_refuse_unsafe_paths_and_unmanaged_units(self):
        for workspace in (self.root / ".." / "escape", self.root / "line\nbreak",
                          self.root / "tab\there", self.config / "overlap"):
            self.assertNotEqual(self.service("install", workspace=workspace)[0], 0)
            self.assertIn("path", self.last_output.lower())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.calls, [])

        foreign = self.root / "foreign"
        foreign.write_text("keep")
        self.unit.parent.mkdir(parents=True)
        for linked in (False, True):
            if linked:
                self.unit.unlink()
                self.unit.symlink_to(foreign)
            else:
                self.unit.write_text("unmanaged")
            for action in ("install", "restart", "remove"):
                self.assertNotEqual(self.service(action)[0], 0)
                self.assertIn("unmanaged", self.last_output)
            self.assertEqual(self.unit.read_text(), "keep" if linked else "unmanaged")
        self.assertEqual(self.calls, [])
        self.unit.unlink()
        self.fragment = str(foreign)
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertFalse(self.unit.exists())

    def test_status_and_failed_supervisor_operations_do_not_claim_success(self):
        self.offline = True
        code, report = self.service("status")
        self.assertNotEqual(code, 0, self.last_output)
        self.assertEqual(report["service"], "unknown")
        self.assertIn("foreground", report["next"])
        self.assertFalse(self.config.exists())
        self.offline = False
        self.fail_reload = True
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertFalse(self.unit.exists())

        self.fail_reload = False
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.drop_ins = "external override"
        self.assertNotEqual(self.service("remove")[0], 0)
        self.assertNotIn("stop", self.calls)
        self.assertTrue(self.unit.exists())

        self.drop_ins = ""
        self.demo()
        with maintenance_lock(self.config / "repairlib-service", exclusive=True):
            self.assertNotEqual(self.service("start")[0], 0)
        self.assertNotIn("start", self.calls)
        self.start_without_process = True
        code, report = self.service("start")
        self.assertNotEqual(code, 0, self.last_output)
        self.assertEqual(report["service"], "stopped")
        registered = self.unit.read_bytes()
        self.fail_reload = True
        self.assertNotEqual(self.service("remove")[0], 0)
        self.assertEqual(self.unit.read_bytes(), registered)
        self.fail_reload = False
        self.assertEqual(self.service("remove")[0], 0, self.last_output)

    def test_foreground_refuses_old_schema_and_serves_with_workspace_lock(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        argv = [*COMMAND, "service", "foreground", "--workspace", str(self.workspace),
                "--port", str(port)]
        env = python_env()
        env["XDG_CONFIG_HOME"] = str(self.config)
        missing = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("setup", missing.stderr)
        self.assertFalse(self.workspace.exists())

        old = self.root / "old"
        path = database_path(old)
        path.parent.mkdir(parents=True)
        initialize_database(path, "Old", "UTC", "owner", "unused", target=1)
        before = path.read_bytes()
        old_argv = [*argv[:-3], str(old), *argv[-2:]]
        refused = subprocess.run(old_argv, env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("migrate", refused.stderr)
        self.assertEqual(path.read_bytes(), before)

        self.demo()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            address = str(self.root / "notify")
            notify.bind(address)
            notify.settimeout(15)
            env["NOTIFY_SOCKET"] = address
            with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as process:
                try:
                    self.assertEqual(notify.recv(128), b"READY=1")
                    url = f"http://127.0.0.1:{argv[-1]}/health"
                    with urlopen(url, timeout=5) as response:
                        self.assertTrue(json.load(response)["ready"])
                    with self.assertRaises(WorkspaceError), maintenance_lock(
                        self.workspace, exclusive=True, create=False
                    ):
                        self.fail("Foreground runtime did not hold the workspace lock")
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    _, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, errors)
        with maintenance_lock(self.workspace, exclusive=True, create=False):
            locked = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(locked.returncode, 0)
        self.assertIn("maintenance", locked.stderr)
        self.assertFalse(self.config.exists())


REAL_RUN = subprocess.run
OPENRC_STATUS = {"started": 0, "stopped": 3, "crashed": 32}


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0, "unprivileged Linux user service")
class OpenRCSystemServiceTests(unittest.TestCase):
    """OpenRC contracts with a fake privileged runner rooted in a private directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "shop 'quoted' \"x\" %n $HOME `id` ; \\ files"
        self.config = self.root / "config"
        self.initd = self.root / "init.d"
        self.runlevels = self.root / "runlevels"
        self.svcdir = self.root / "openrc"
        self.pidfile = self.root / "run" / "repairlib.pid"
        self.openrc_run = self.root / "openrc-run"
        for directory in (self.initd, self.runlevels / "default", self.svcdir / "started", self.pidfile.parent,
                          self.svcdir / "options" / "repairlib"):
            directory.mkdir(parents=True)
        self.initd.chmod(0o755)
        (self.runlevels / "default").chmod(0o755)
        self.openrc_run.write_text("")
        self.script = self.initd / "repairlib"
        self.entry = self.runlevels / "default" / "repairlib"
        self.owner = (os.getuid(), os.getgid())
        self.identity = None
        self.tools = True
        self.privileges = {"doas"}
        self.state = "stopped"
        self.healthy = True
        self.start_crashes = False
        self.failures: dict[object, bool] = {}
        self.calls = []

    # Fake OpenRC and privilege tools; nothing here reaches /etc, doas, sudo or rc-service.
    def runner(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] in ("doas", "sudo"):
            if argv[1:] in (["-n", "true"], ["-n", "rc-update", "show", "default"]):
                if argv[0] not in self.privileges:
                    raise FileNotFoundError(argv[0])
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.assertIn(argv[0], self.privileges)
            self.assertEqual(argv[1], "-n")
            return self.root_command(argv[2:])
        if argv[:3] == ["rc-service", "repairlib", "status"]:
            self.assertTrue(self.script.is_file())
            if self.failures.get("status"):
                return subprocess.CompletedProcess(argv, 1, "", "denied")
            return subprocess.CompletedProcess(argv, OPENRC_STATUS[self.state], "", "")
        if argv[:2] == ["sh", "-n"]:
            return REAL_RUN(argv, **kwargs)
        if argv[0] == sys.executable and argv[-2:] == ["service", "--help"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.fail(f"Unexpected command {argv}")

    def root_command(self, argv):
        done = subprocess.CompletedProcess(argv, 0, "", "")
        failed = subprocess.CompletedProcess(argv, 1, "", "failed")
        if self.failures.get(argv[0]) or self.failures.get(tuple(argv[:2])):
            return failed
        if argv[0] == "install":
            self.assertEqual(argv[1:7], ["-o", "root", "-g", "root", "-m", "0755"])
            source, target = map(Path, argv[7:])
            self.assertEqual(target.parent, self.initd)
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
        elif argv[0] == "ln":
            self.assertEqual(Path(argv[2]), self.script)
            try:
                os.link(argv[1], argv[2])
            except FileExistsError:
                return failed
        elif argv[0] == "rm":
            target = Path(argv[-1])
            self.assertEqual(target.parent, self.initd)
            if argv[1] != "-f" or target.exists():
                target.unlink()
        elif argv[0] == "rc-update":
            self.assertEqual(argv[2:], ["repairlib", "default"])
            if argv[1] == "add":
                self.entry.symlink_to(self.script)
            else:
                self.entry.unlink()
        elif argv[:2] == ["rc-service", "repairlib"]:
            marker = self.svcdir / "started" / "repairlib"
            child = self.svcdir / "options" / "repairlib" / "child_pid"
            if argv[2] in ("start", "restart") and not self.start_crashes:
                self.state = "started"
                marker.write_text("")
                self.pidfile.write_text(f"{os.getpid()}\n")
                child.write_text(f"{os.getpid()}\n")
            elif argv[2] in ("start", "restart", "stop"):
                self.state = "stopped"
                for path in (marker, self.pidfile, child):
                    path.unlink(missing_ok=True)
            else:
                self.fail(f"Unexpected privileged command {argv}")
        else:
            self.fail(f"Unexpected privileged command {argv}")
        return done

    def service(self, action, *, workspace=None, port="8092", extra=()):
        output, errors = io.StringIO(), io.StringIO()
        args = ["service", action, "--openrc-system", "--workspace", str(workspace or self.workspace),
                "--port", port, *extra]
        module = "repairlib.lifecycle.service."
        with contextlib.ExitStack() as stack:
            for name, value in (("OPENRC_INITD", self.initd), ("OPENRC_RUNLEVELS", self.runlevels),
                                ("OPENRC_SVCDIR", self.svcdir), ("OPENRC_PIDFILE", self.pidfile),
                                ("OPENRC_RUN", self.openrc_run), ("OPENRC_OWNER", self.owner),
                                ("READY_TIMEOUT", 0.2), ("READY_INTERVAL", 0.01)):
                stack.enter_context(patch(module + name, value))
            stack.enter_context(patch(module + "which",
                                      lambda name: f"/sbin/{name}" if self.tools else None))
            stack.enter_context(patch(module + "health_ready",
                                      lambda port: self.healthy and self.state == "started"
                                      or self.healthy == "always"))
            stack.enter_context(patch(module + "owns_listener", lambda pid, port: True))
            if self.identity:
                stack.enter_context(patch(module + "current_identity", lambda: self.identity))
            stack.enter_context(patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}))
            stack.enter_context(patch("subprocess.run", side_effect=self.runner))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            try:
                code = main(args)
            except SystemExit as error:
                code = error.code
        self.last_output = output.getvalue() + errors.getvalue()
        return code, json.loads(output.getvalue()) if output.getvalue() else {}

    def privileged_calls(self):
        return [call for call in self.calls if call[0] in ("doas", "sudo")
                and call[2:] not in (["true"], ["rc-update", "show", "default"])]

    def demo(self):
        initialize_demo_workspace(self.workspace, username="owner", password=PASSWORD)

    def test_find_process_matches_private_openrc_metadata(self):
        proc = self.root / "proc"
        for pid, argv in ((111, b"supervise-daemon\0repairlib\0"), (222, b"python\0service\0foreground\0")):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "cmdline").write_bytes(argv)
        with patch("repairlib.lifecycle.service.OPENRC_PROC", proc):
            self.assertEqual(
                find_process(lambda argv: argv == ["supervise-daemon", "repairlib"], os.getuid()), 111)
            self.assertEqual(
                find_process(lambda argv: argv == ["python", "service", "foreground"], os.getuid()), 222)

    def test_control_directory_drops_inherited_setgid(self):
        self.config.mkdir()
        self.config.chmod(0o2750)
        code, report = self.service("install")
        self.assertEqual(code, 0, self.last_output)
        self.assertEqual((self.config / "repairlib-openrc").stat().st_mode & 0o777, 0o700)

    def test_refuses_repeated_space_workspace_for_openrc(self):
        workspace = self.root / "shop  double"
        code, report = self.service("install", workspace=workspace)
        self.assertNotEqual(code, 0, self.last_output)
        self.assertIn("repeated spaces", report["error"])
        self.assertEqual(self.privileged_calls(), [])

    def test_install_lifecycle_runs_unprivileged_user_under_root_supervision(self):
        code, report = self.service("install")
        self.assertEqual(code, 0, self.last_output)
        self.assertEqual((report["backend"], report["service"], report["workspace_state"]),
                         ("openrc", "stopped", "missing"))
        self.assertEqual((report["script"], report["name"], report["runlevel"]),
                         (str(self.script), "repairlib", "default"))
        self.assertEqual(report["url"], "http://127.0.0.1:8092")
        self.assertIn("setup", report["next"])
        self.assertTrue(report["in_runlevel"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.calls[0], ["doas", "-n", "true"])
        self.assertEqual([call[2] for call in self.privileged_calls()], ["install", "ln", "rm", "rc-update"])
        self.assertFalse(any(call[0] == "sudo" for call in self.calls))
        info = self.script.lstat()
        self.assertEqual((info.st_mode & 0o7777, info.st_nlink), (0o755, 1))
        self.assertEqual(os.readlink(self.entry), str(self.script))
        self.assertEqual(sorted(path.name for path in self.initd.iterdir()), ["repairlib"])
        self.assertEqual((self.config / "repairlib-openrc").stat().st_mode & 0o777, 0o700)

        # The script round-trips every argument exactly through the shell evaluation
        # supervise-daemon performs, so workspace metacharacters cannot inject commands.
        text = self.script.read_text()
        user, group = report["user"].split(":")
        self.assertIn(f"command_user={user}:{group}\n", text)
        self.assertIn("supervisor=supervise-daemon\n", text)
        self.assertIn(f"pidfile={self.pidfile}\n", text)
        words = REAL_RUN(["sh", "-c", '. "$1"; eval "set -- $command $command_args"; printf "%s\\0" "$@"',
                          "sh", str(self.script)], capture_output=True, check=True).stdout
        argv = words.decode().split("\0")[:-1]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-6:], ["service", "foreground", "--workspace", str(self.workspace),
                                     "--port", "8092"])

        self.calls.clear()
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.assertEqual(self.privileged_calls(), [])
        self.assertNotEqual(self.service("start")[0], 0)
        self.assertNotIn(["doas", "-n", "rc-service", "repairlib", "start"], self.calls)

        self.demo()
        before = database_path(self.workspace).read_bytes()
        for action, expected in (("start", "running"), ("status", "running"),
                                 ("restart", "running"), ("stop", "stopped"), ("status", "stopped"),
                                 ("start", "running"), ("remove", "absent"), ("status", "absent")):
            self.calls.clear()
            code, report = self.service(action)
            self.assertEqual(code, 0, self.last_output)
            self.assertEqual(report["service"], expected, action)
            self.assertEqual(report["workspace_state"], "initialized")
            self.assertEqual(report["pid"], os.getpid() if expected == "running" else None, action)
            if action == "status":
                self.assertEqual(self.privileged_calls(), [])
                self.assertFalse(any(call[1:] == ["-n", "true"] for call in self.calls))
        self.assertFalse(os.path.lexists(self.script))
        self.assertFalse(os.path.lexists(self.entry))
        self.assertEqual(database_path(self.workspace).read_bytes(), before)
        self.assertTrue((self.config / "repairlib-openrc" / ".maintenance.lock").is_file())

    def test_stock_source_cutover_can_repeat_without_losing_post_cutover_writes(self):
        self.demo()
        now = int(time())
        session = sessions.login(self.workspace, "owner", PASSWORD, now=now)
        self.assertIsNotNone(session)
        token = tokens.create_token(self.workspace, session, password=PASSWORD, name="Cutover fixture",
                                    scope="write", key="a" * 32, now=now)
        self.assertIsNotNone(token)
        auth = {"Authorization": f"Bearer {token}"}
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        sources = {label: self.root / label / "src" for label in "ABC"}
        for source in sources.values():
            shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))

        def service(action, label, succeeds=True):
            with patch("repairlib.lifecycle.service.SOURCE", sources[label]):
                code, report = self.service(action, port=str(port))
            self.assertEqual(code == 0, succeeds, self.last_output)
            self.assertEqual(report["source"], str(sources[label]))
            return report

        def offline(*args):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = main([str(arg) for arg in args])
            self.assertEqual(result, 0, output.getvalue())

        @contextlib.contextmanager
        def listener(label, *, workspace=None):
            if workspace is None:
                fields = dict(line.split("=", 1) for line in self.script.read_text().splitlines()
                              if line.startswith(("command=", "command_args=")))
                argv = [fields["command"], *shlex.split(shlex.split(fields["command_args"])[0])]
                workspace = self.workspace
                self.assertTrue(self.entry.is_symlink())
            else:
                argv = [*python_args(Path(sys.executable), sources[label], BOOT),
                        *foreground_args(workspace, port)]
            self.assertIn(str(sources[label]), argv)
            self.assertIn(str(workspace), argv)
            with running_listener(argv, port) as (client, _):
                client.headers.update(auth)
                yield client

        def write(client, endpoint, key, **body):
            response = client.post(endpoint, headers={"Idempotency-Key": key}, **body)
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()["id"]

        def add_repair(client, label):
            customer = write(client, "/api/v1/customers", label + "customer", json={"name": "Synthetic " + label})
            repair = write(client, "/api/v1/repairs", label + "repair",
                           json={"customer_id": customer, "reported_issue": "Synthetic issue"})
            path = f"/api/v1/repairs/{repair}/attachments"
            original = b"\x00synthetic " + label.encode() + b"\xff"
            attachment = write(client, path, label + "attachment", params={"filename": "original.bin"},
                               content=original)
            return customer, f"{path}/{attachment}/content", original, label

        def read_back(client, records):
            for customer, attachment, original, label in records:
                response = client.get(f"/api/v1/customers/{customer}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["name"], "Synthetic " + label)
                response = client.get(attachment)
                self.assertEqual((response.status_code, response.content), (200, original))

        service("install", "A")
        service("start", "A")
        with listener("A") as client:
            records = [add_repair(client, "A")]
        archives = {}
        for old, new in (("A", "B"), ("B", "C")):
            service("stop", old)
            registered = self.script.read_bytes()
            self.calls.clear()
            service("install", new, succeeds=False)
            self.assertEqual(self.privileged_calls(), [])
            self.assertEqual(self.script.read_bytes(), registered)

            folder = self.root / f"recovery-{old}"
            folder.mkdir(mode=0o700)
            backup, restored = folder / "backup.zip", folder / "restored"
            offline("backup", "create", "--workspace", self.workspace, "--output", backup)
            offline("backup", "verify", backup)
            offline("backup", "restore", backup, "--workspace", restored)
            offline("doctor", "--workspace", restored)
            archives[backup] = backup.read_bytes()
            with listener(new, workspace=restored) as client:
                read_back(client, records)

            service("remove", old)
            service("install", new)
            if new == "C":
                self.start_crashes = True
                service("start", new, succeeds=False)
                self.start_crashes = False
                service("remove", new)
                service("install", old)
                service("start", old)
                with listener(old) as client:
                    read_back(client, records)
                service("remove", old)
                service("install", new)
            self.assertEqual(service("start", new)["service"], "running")
            with listener(new) as client:
                read_back(client, records)
                if new == "B":
                    records.append(add_repair(client, "B"))

        service("stop", "C")
        for backup, content in archives.items():
            self.assertEqual(backup.read_bytes(), content)
            offline("backup", "verify", backup)

    def test_sudo_fallback_and_missing_privilege_tool(self):
        self.privileges = set()
        code, report = self.service("install")
        self.assertNotEqual(code, 0)
        self.assertIn("doas or sudo", report["error"])
        self.assertEqual(self.calls, [
            ["doas", "-n", "true"],
            ["doas", "-n", "rc-update", "show", "default"],
            ["sudo", "-n", "true"],
            ["sudo", "-n", "rc-update", "show", "default"],
        ])
        self.assertEqual(list(self.initd.iterdir()), [])
        self.assertFalse(os.path.lexists(self.entry))
        self.assertFalse(self.config.exists())

        self.privileges = {"sudo"}
        self.calls.clear()
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.assertEqual({call[0] for call in self.privileged_calls()}, {"sudo"})

    def test_refuse_preexisting_unmanaged_scripts_without_stop_or_remove(self):
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        managed = self.script.read_bytes()
        self.entry.unlink()
        self.script.unlink()
        foreign = self.root / "foreign"
        foreign.write_bytes(managed)

        def unmanaged():
            self.script.write_text("#!/sbin/openrc-run\ncommand=/bin/other\n")
            self.script.chmod(0o755)

        def linked():
            self.script.symlink_to(foreign)

        def wrong_mode():
            self.script.write_bytes(managed)
            self.script.chmod(0o775)

        def hardlinked():
            os.link(foreign, self.script)
            self.script.chmod(0o755)

        def other_port():
            self.script.write_bytes(managed.replace(b"8092", b"8093"))
            self.script.chmod(0o755)

        for case in (unmanaged, linked, wrong_mode, hardlinked, other_port):
            case()
            before = self.script.read_bytes()
            for action in ("install", "start", "restart", "stop", "remove", "status"):
                self.calls.clear()
                code, report = self.service(action)
                self.assertNotEqual(code, 0, (case.__name__, action))
                self.assertIn("unmanaged", report["error"], (case.__name__, action))
                self.assertEqual(self.calls, [], (case.__name__, action))
            self.assertEqual(self.script.read_bytes(), before)
            self.assertFalse(os.path.lexists(self.entry))
            self.script.unlink()
        self.assertEqual(self.service("install")[1]["service"], "stopped")
        self.entry.unlink()
        self.script.unlink()

        # Wrong owner: the script's group no longer matches the expected root group.
        self.script.write_bytes(managed)
        self.script.chmod(0o755)
        self.owner = (os.getuid(), os.getgid() + 4242)
        self.calls.clear()
        code, report = self.service("remove")
        self.assertNotEqual(code, 0)
        self.assertIn("unmanaged", report["error"])
        self.assertEqual(report["existing_sha256"], __import__("hashlib").sha256(managed).hexdigest())
        self.assertEqual(self.calls, [])
        self.owner = (os.getuid(), os.getgid())
        self.script.unlink()

        # A leftover runlevel entry or started marker without the script is refused too.
        self.entry.symlink_to(self.script)
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertIn("without its script", self.last_output)
        self.entry.unlink()
        self.entry.symlink_to(self.root / "elsewhere")
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertIn("foreign", self.last_output)
        self.assertEqual(self.privileged_calls(), [])
        self.assertFalse(os.path.lexists(self.script))

    def test_reject_unsafe_inputs_and_hosts_before_privileged_calls(self):
        for workspace in (self.root / ".." / "escape", self.root / "line\nbreak",
                          self.root / "tab\there", self.config / "overlap",
                          self.initd / "overlap", self.svcdir / "overlap"):
            self.assertNotEqual(self.service("install", workspace=workspace)[0], 0)
            self.assertIn("path", self.last_output.lower())
        for port in ("0", "65536", "-1"):
            self.assertNotEqual(self.service("install", port=port)[0], 0)
            self.assertIn("port", self.last_output)
        self.assertEqual(self.service("install", port="80x")[0], 2)
        for identity in (("root", "root"), ("bad user", "staff"), ("ian", "x:y"), ("ian\n", "ian")):
            self.identity = identity
            self.assertNotEqual(self.service("install")[0], 0)
            self.assertIn("user and group", self.last_output)
        self.identity = None
        with patch("sys.executable", str(self.root / "python 3" / "python")):
            self.assertNotEqual(self.service("install")[0], 0)
            self.assertIn("only letters", self.last_output)
        self.tools = False
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertIn("OpenRC", self.last_output)
        self.tools = True
        self.initd.chmod(0o775)
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertIn("root-owned OpenRC", self.last_output)
        self.initd.chmod(0o755)
        self.svcdir.rename(self.root / "not-booted")
        self.assertNotEqual(self.service("status")[0], 0)
        self.assertIn("not the running service manager", self.last_output)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.initd.iterdir()), [])
        self.assertFalse(self.config.exists())

    def test_install_failures_roll_back_only_what_they_created(self):
        self.failures = {"ln": True}
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertEqual(list(self.initd.iterdir()), [])
        self.failures = {("rc-update", "add"): True}
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertEqual(list(self.initd.iterdir()), [])
        self.assertFalse(os.path.lexists(self.entry))
        self.calls.clear()
        self.failures = {"status": True}
        code, report = self.service("install")
        self.assertNotEqual(code, 0)
        self.assertEqual(report["service"], "unknown")
        self.assertEqual([call[2:4] for call in self.privileged_calls()][-2:],
                         [["rc-update", "del"], ["rm", "-f"]])
        self.assertEqual(list(self.initd.iterdir()), [])
        self.assertFalse(os.path.lexists(self.entry))

        # A failed repair of runlevel membership keeps the pre-existing script.
        self.failures = {}
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.entry.unlink()
        self.failures = {("rc-update", "add"): True}
        self.assertNotEqual(self.service("install")[0], 0)
        self.assertTrue(self.script.is_file())

        # A failed remove restores the runlevel entry it removed and keeps the script.
        self.failures = {}
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.failures = {"rm": True}
        self.assertNotEqual(self.service("remove")[0], 0)
        self.assertTrue(self.script.is_file())
        self.assertEqual(os.readlink(self.entry), str(self.script))
        self.failures = {}
        self.assertEqual(self.service("remove")[1]["service"], "absent")

    def test_readiness_timeout_and_foreign_listener_never_report_running(self):
        self.assertEqual(self.service("install")[0], 0, self.last_output)
        self.demo()
        self.healthy = False
        start = __import__("time").monotonic()
        code, report = self.service("start")
        self.assertLess(__import__("time").monotonic() - start, 5)
        self.assertNotEqual(code, 0)
        self.assertEqual((report["service"], report["supervisor_state"], report["health"]),
                         ("unready", "started", "unavailable"))
        self.assertIn("healthy", report["error"])
        self.assertEqual(self.service("status")[1]["service"], "unready")
        self.assertEqual(self.service("stop")[1]["service"], "stopped")

        self.healthy = True
        self.start_crashes = True
        code, report = self.service("restart")
        self.assertNotEqual(code, 0)
        self.assertEqual(report["service"], "stopped")

        self.start_crashes = False
        self.healthy = "always"
        self.calls.clear()
        code, report = self.service("start")
        self.assertNotEqual(code, 0)
        self.assertIn("Another listener", report["error"])
        self.assertNotIn("start", [call[-1] for call in self.privileged_calls()])

    def test_foreground_and_root_are_rejected(self):
        code, _ = self.service("foreground")
        self.assertNotEqual(code, 0)
        self.assertIn("--openrc-system", self.last_output)
        with patch("os.geteuid", return_value=0):
            self.assertNotEqual(self.service("install")[0], 0)
        self.assertIn("unprivileged", self.last_output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.config.exists())
