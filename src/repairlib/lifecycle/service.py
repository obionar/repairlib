"""One Linux systemd user unit, or one OpenRC system service, for a selected local runtime."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, suppress
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
from secrets import token_hex
import shlex
from shutil import which
import socket
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time

from repairlib.lifecycle.checkout import PENDING, READY
from repairlib.lifecycle.workspace import SETUP_STATES, WorkspaceError, maintenance_lock, workspace_schema
from repairlib.storage.initialization import SCHEMA_VERSION

UNIT = "repairlib.service"
SOURCE = Path(__file__).resolve().parents[2]
PROPERTIES = ("LoadState", "ActiveState", "SubState", "MainPID",
              "FragmentPath", "DropInPaths", "NeedDaemonReload")
BOOT = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from repairlib.__main__ import main; raise SystemExit(main())"
)


class SupervisorError(WorkspaceError):
    """The user manager could not complete or confirm an operation."""


def check_path(path: Path, *, canonical: bool = True) -> Path:
    if (not path.is_absolute() or ".." in path.parts or not str(path).isprintable()
            or (canonical and path.resolve() != path)):
        raise WorkspaceError("Service paths must be absolute, with no links, traversal or control characters.")
    return path


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def quote_unit_arg(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def foreground_args(workspace: Path, port: int, *, managed: bool = False,
                    lan: bool = False) -> tuple[str, ...]:
    return ("service", "managed-foreground" if managed else "foreground",
            "--workspace", str(workspace), "--port", str(port), *(("--lan",) if lan else ()))


def python_args(executable: Path, source: Path, boot: str) -> tuple[str, ...]:
    return (str(executable), "-I", "-B", "-c", boot, str(source))


def service_launchers(executable: Path) -> tuple[tuple[str, ...], ...]:
    """Preferred registration first, followed by exact compatible legacy launchers."""
    legacy = python_args(executable, SOURCE, BOOT)
    checkout = SOURCE.parent
    launcher = checkout / "bin" / "repairlib"
    if not (checkout / ".git").exists() or not launcher.is_file():
        # Versioned installations have their own launcher and handoff protocol.
        return (legacy,)
    check_path(launcher)
    return ((str(executable), "-I", "-B", str(launcher)), legacy)


def unit_bytes(argv: tuple[str, ...]) -> bytes:
    command = " ".join(map(quote_unit_arg, argv))
    # The colon disables systemd's environment substitution in argv.
    return (
        "[Unit]\nDescription=RepairLib local repair shop\n\n"
        "[Service]\nType=notify\nNotifyAccess=main\nUMask=0077\n"
        "TimeoutStartSec=25s\nTimeoutStopSec=15s\n"
        f"ExecStart=:{command}\n"
    ).encode()


def registered_unit(unit: Path, *expected: bytes) -> bytes | None:
    try:
        fd = os.open(unit, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorkspaceError("Refusing an unmanaged or inaccessible service unit.") from error
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise WorkspaceError("Refusing an unmanaged service unit or different workspace/runtime/port.")
        content = file.read(max(map(len, expected)) + 1)
        if content not in expected:
            raise WorkspaceError("Refusing an unmanaged service unit or different workspace/runtime/port.")
    return content


def publish_unit(unit: Path, content: bytes) -> None:
    with NamedTemporaryFile(dir=unit.parent, prefix=".repairlib-") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
        os.link(file.name, unit)


def systemctl(action: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", action, *args], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SupervisorError("The systemd user manager is unavailable or timed out.") from error


def read_unit(unit: Path, present: bool, *, allow_reload: bool = False) -> dict[str, str]:
    result = systemctl("show", "--all", "--property=" + ",".join(PROPERTIES), UNIT)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    absent = (values.get("LoadState") == "not-found" and values.get("ActiveState") == "inactive"
              and values.get("MainPID") == "0")
    if (not set(PROPERTIES) <= values.keys() or not values["MainPID"].isascii()
            or not values["MainPID"].isdigit()
            or (result.returncode and not (result.returncode == 4 and absent))):
        raise SupervisorError("Could not read service state from the systemd user manager.")
    fragment = values["FragmentPath"]
    if (values["DropInPaths"] or (fragment and (not present or fragment != str(unit)))
            or (not absent and (values["LoadState"] != "loaded" or not fragment))):
        raise WorkspaceError("Refusing an unmanaged, overridden or invalid unit in the user manager.")
    if not allow_reload and (absent == present or values["NeedDaemonReload"] != "no"):
        raise SupervisorError("The user manager and unit file disagree. Retry service install to reload.")
    return values


def apply_systemctl(action: str, *args: str) -> None:
    if systemctl(action, *args).returncode:
        raise SupervisorError(f"systemd user {action} failed. Check service status and the user journal.")


def setup_hint(state: str) -> str | None:
    if state in SETUP_STATES:
        return "Run setup for this workspace, then start the service."
    if state == "upgrade_required":
        return "Stop the service, verify a backup, and run explicit migrate for this workspace."
    if state != "initialized":
        return "Stop the service and run doctor for this workspace."
    return None


def create_private_dirs(path: Path) -> None:
    """Create *path* and its missing parents one level at a time, all mode 0700.

    Path.mkdir(parents=True) applies mode only to the final component, leaving
    intermediate parents with the process umask (0775 with umask 002). Those
    group-writable parents make service_paths refuse every later command and let
    a shared group tamper with the unit path. Walk from the deepest existing
    ancestor down instead, and verify every new directory through a no-follow fd.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.parent == current:
            raise WorkspaceError("Service configuration directories must be private (0700) and user owned.")
        missing.append(current)
        current = current.parent
    ancestor = current.lstat()
    if (not stat.S_ISDIR(ancestor.st_mode) or ancestor.st_uid != os.getuid()
            or ancestor.st_mode & 0o022):
        raise WorkspaceError("Service configuration directories must be private (0700) and user owned.")
    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            raise WorkspaceError("Service configuration directories must be private (0700) and user owned.") from error
        try:
            if created:
                # A setgid parent makes mkdir inherit setgid; strip it without following a race.
                os.fchmod(fd, 0o700)
            info = os.fstat(fd)
        finally:
            os.close(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise WorkspaceError("Service configuration directories must be private (0700) and user owned.")


def private_control(control: Path, *, create: bool) -> bool:
    if create:
        create_private_dirs(control.parent)
        try:
            control.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            # A setgid parent makes mkdir inherit setgid; strip it without following a race.
            fd = os.open(control, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
    if not control.exists():
        return False
    info = control.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise WorkspaceError("The service control directory must be private (0700) and user owned.")
    return True


def foreground(workspace: Path, port: int, *, listener_state: Callable[[], dict] | None = None,
               listener_id: str | None = None, lan: bool = False) -> int:
    # Supervised restarts (including at boot) must not start a half-updated checkout.
    pending = SOURCE.parent / ".git" / PENDING
    if os.path.lexists(pending):
        try:
            state = json.loads(pending.read_text(encoding="utf8"))
            verified = isinstance(state, dict) and state.get("stage") == READY
        except (OSError, ValueError):
            verified = False
        if not verified:
            raise WorkspaceError("Checkout update is pending. See interrupted checkout updates in docs/operations.md.")
    state, _ = workspace_schema(workspace)
    if hint := setup_hint(state):
        raise WorkspaceError(hint)
    import uvicorn

    from repairlib.app import create_app

    class ServiceServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started and (address := os.environ.get("NOTIFY_SOCKET")):
                if address.startswith("@"):
                    address = "\0" + address[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", address)

    server = ServiceServer(uvicorn.Config(
        create_app(workspace=workspace, require_initialized=True,
                   listener_state=listener_state, listener_id=listener_id, lan=lan),
        # LAN mode binds every IPv4 interface, loopback included, so local probes still answer.
        host="0.0.0.0" if lan else "127.0.0.1", port=port, log_level="info",
        access_log=False, proxy_headers=False, ws="none",
    ))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0 if server.started else 1


def service_paths(workspace: Path) -> tuple[Path, Path]:
    config = check_path(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))
    unit = check_path(config / "systemd" / "user") / UNIT
    control = check_path(config / "repairlib-service")
    if overlaps(workspace, config):
        raise WorkspaceError("Workspace and service configuration paths must be separate.")
    for directory in (config, unit.parent.parent, unit.parent):
        if directory.exists():
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                raise WorkspaceError("Service configuration directories must be user owned and protected from other writers.")
    return unit, control


def manage(action: str, workspace: Path, port: int, lan: bool = False) -> int:
    unit, control = service_paths(workspace)
    executable = check_path(Path(sys.executable), canonical=False)
    contents = tuple(unit_bytes(launcher + foreground_args(workspace, port, lan=lan))
                     for launcher in service_launchers(executable))
    state, version = workspace_schema(workspace)
    report = {
        "unit": str(unit), "service": "unknown", "workspace": str(workspace),
        "workspace_state": state, "schema_version": version, "expected_schema_version": SCHEMA_VERSION,
        "python": str(executable), "source": str(SOURCE),
        "url": f"http://127.0.0.1:{port}", "logs": f"journalctl --user -u {UNIT}",
        "next": setup_hint(state),
    }
    try:
        if action == "install" and state in {"invalid", "unsupported_schema"}:
            raise WorkspaceError(setup_hint(state))
        with ExitStack() as stack:
            # Preflight a new installation before creating the private control directory.
            if action == "install" and not control.exists():
                read_unit(unit, bool(registered_unit(unit, *contents)), allow_reload=True)
            locked = private_control(control, create=action == "install")
            if locked:
                stack.enter_context(maintenance_lock(
                    control, exclusive=action != "status", create=action == "install",
                ))
            existing = registered_unit(unit, *contents)
            present = existing is not None
            if present and not locked:
                raise WorkspaceError("The unit has no service lock. Retry service install to register it.")
            current = read_unit(unit, present, allow_reload=action == "install")
            if action == "install":
                if not present:
                    create_private_dirs(unit.parent)
                    publish_unit(unit, contents[0])
                if not present or current["LoadState"] != "loaded" or current["NeedDaemonReload"] != "no":
                    try:
                        apply_systemctl("daemon-reload")
                        current = read_unit(unit, True)
                    except WorkspaceError:
                        if not present:
                            unit.unlink()
                            with suppress(SupervisorError):
                                apply_systemctl("daemon-reload")
                        raise
            elif action != "status":
                if not present and action != "remove":
                    raise WorkspaceError("Run service install before controlling this unit.")
                if action in {"start", "restart"}:
                    if hint := setup_hint(workspace_schema(workspace)[0]):
                        raise WorkspaceError(hint)
                if existing is not None:
                    apply_systemctl("stop" if action == "remove" else action, UNIT)
                    current = read_unit(unit, True)
                    if action == "remove" and current["ActiveState"] == "inactive" and current["MainPID"] == "0":
                        unit.unlink()
                        try:
                            apply_systemctl("daemon-reload")
                            current = read_unit(unit, False)
                        except WorkspaceError:
                            publish_unit(unit, existing)
                            with suppress(SupervisorError):
                                apply_systemctl("daemon-reload")
                            raise

            active, substate = current["ActiveState"], current["SubState"]
            pid = int(current["MainPID"])
            observed = active
            if current["LoadState"] == "not-found":
                observed = "absent"
            elif active == "active":
                observed = "running" if substate == "running" and pid else "unknown"
            elif active == "inactive" and not pid:
                observed = "stopped"
            # systemd's Type=notify only proves the process signalled readiness; confirm the
            # same application-level facts OpenRC checks before calling a running unit healthy.
            ready = None
            unready_error = None
            if observed == "running":
                listener = owns_listener(pid, port)
                healthy = health_ready(port)
                ready = listener and healthy
                if not ready:
                    observed = "unready"
                    failed = [name for name, ok in (("listener", listener), ("health", healthy)) if not ok]
                    unready_error = "RepairLib is active but application readiness failed: " + ", ".join(failed) + "."
            report.update(service=observed, active_state=active, substate=substate, pid=pid,
                          health=None if ready is None else ("ready" if ready else "unavailable"))
            if unready_error is not None:
                if action in {"start", "restart"}:
                    raise SupervisorError(unready_error)
                report["error"] = unready_error
            expected = {"start": "running", "restart": "running", "stop": "stopped", "remove": "absent"}
            if observed == "unknown" or (action in expected and observed != expected[action]):
                raise SupervisorError("The user manager did not confirm the requested state.")
    except WorkspaceError as error:
        report["error"] = str(error)
        if isinstance(error, SupervisorError) and report["service"] == "unknown":
            report["next"] = "If the user manager is unavailable, use service foreground after setup."
        print(json.dumps(report))
        return 1
    print(json.dumps(report))
    return 0


# OpenRC system service (Alpine). Root only supervises: supervise-daemon drops to the
# invoking user before exec. Every location below is a module constant so tests can
# redirect it into a private directory without touching /etc, /run or a real service.
OPENRC_SERVICE = "repairlib"
OPENRC_INITD = Path("/etc/init.d")
OPENRC_RUNLEVELS = Path("/etc/runlevels")
OPENRC_RUNLEVEL = "default"
OPENRC_SVCDIR = Path("/run/openrc")
OPENRC_PIDFILE = Path("/run/repairlib.pid")
OPENRC_RUN = Path("/sbin/openrc-run")
OPENRC_PROC = Path("/proc")
OPENRC_OWNER = (0, 0)
OPENRC_TOOLS = ("rc-service", "rc-update", "supervise-daemon")
PRIVILEGE_TOOLS = ("doas", "sudo")
READY_TIMEOUT = 30.0
READY_INTERVAL = 0.5
OPENRC_EXIT = {0: "started", 3: "stopped", 4: "stopping", 8: "starting", 16: "inactive", 32: "crashed"}
OPENRC_MARKERS = ("started", "starting", "stopping", "inactive", "failed", "wasinactive")
SAFE_TOKEN = re.compile(r"/[A-Za-z0-9._/+-]+")
SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
UNMANAGED_SCRIPT = "Refusing an unmanaged OpenRC service script or different workspace/runtime/port."


class UnmanagedScript(WorkspaceError):
    """An existing init script is not the exact RepairLib script; its preimage is kept."""

    def __init__(self, message: str, preimage: str | None = None):
        super().__init__(message)
        self.preimage = preimage


def current_identity() -> tuple[str, str]:
    import grp
    import pwd

    entry = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(entry.pw_gid).gr_name
    return entry.pw_name, group


def openrc_script(argv: tuple[str, ...], user: str, group: str) -> bytes:
    command, args = argv[0], argv[1:]
    for token in (command, str(OPENRC_PIDFILE), str(OPENRC_RUN)):
        if not SAFE_TOKEN.fullmatch(token):
            raise WorkspaceError("OpenRC runtime paths may contain only letters, digits and ._/+- characters.")
    if not (SAFE_NAME.fullmatch(user) and SAFE_NAME.fullmatch(group)) or user == "root":
        raise WorkspaceError("OpenRC services need an unprivileged user and group with portable names.")
    if any("  " in arg or any(character in arg for character in "*?[") for arg in argv):
        raise WorkspaceError("OpenRC paths cannot contain repeated spaces or glob characters.")
    # supervise-daemon.sh evals $command_args once after expansion, so its value is a
    # shell-quoted word list; the assignment then single-quotes that whole value.
    words = " ".join(map(shlex.quote, args))
    return (
        f"#!{OPENRC_RUN}\n"
        "# Managed by RepairLib. Remove with: repairlib service remove --openrc-system\n"
        'name="RepairLib"\n'
        'description="RepairLib local repair shop"\n'
        "supervisor=supervise-daemon\n"
        f"command={command}\n"
        f"command_args={shlex.quote(words)}\n"
        f"command_user={user}:{group}\n"
        f"pidfile={OPENRC_PIDFILE}\n"
        "umask=077\n"
        "respawn_delay=2\nrespawn_max=5\nrespawn_period=60\n"
        'output_logger="logger -t repairlib -p daemon.info"\n'
        'error_logger="logger -t repairlib -p daemon.err"\n'
        "\ndepend() {\n\tneed localmount\n\tuse logger\n\tafter net\n}\n"
    ).encode()


def registered_script(script: Path, *expected: bytes) -> bytes | None:
    try:
        fd = os.open(script, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UnmanagedScript(UNMANAGED_SCRIPT + " It is linked or inaccessible.") from error
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        content = file.read(1 << 20) if stat.S_ISREG(info.st_mode) else b""
        if (not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != OPENRC_OWNER
                or stat.S_IMODE(info.st_mode) != 0o755 or info.st_nlink != 1 or content not in expected):
            raise UnmanagedScript(UNMANAGED_SCRIPT, sha256(content).hexdigest() if content else None)
    return content


def openrc_command(argv: list[str], *, timeout: float = 45) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SupervisorError(f"{argv[0]} is unavailable or timed out.") from error


def privilege_tool() -> str:
    # A command-limited doas/sudo rule does not usually authorize `true`; probe
    # a harmless command from the documented OpenRC set as well.
    probes = (["-n", "true"], ["-n", "rc-update", "show", "default"])
    for tool in PRIVILEGE_TOOLS:
        for probe in probes:
            try:
                result = subprocess.run([tool, *probe], stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                return tool
    raise WorkspaceError(
        "OpenRC system service changes need non-interactive doas or sudo. Allow this user to run "
        "install, ln, rm, rc-update (including `show default`) and rc-service without a password "
        "(doas: permit nopass; sudo: NOPASSWD), then retry. RepairLib never prompts for or stores "
        "credentials."
    )


def privileged(tool: str, *argv: str) -> None:
    if openrc_command([tool, "-n", *argv]).returncode:
        raise SupervisorError(f"Privileged {' '.join(argv[:2])} failed. Check rc-service {OPENRC_SERVICE} status.")


def read_pid(path: Path, uid: int) -> int | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as file:
        text = file.read(32).strip()
    if not text.isdigit() or not 0 < int(text) < 1 << 22:
        return None
    try:
        return int(text) if (OPENRC_PROC / text.decode()).stat().st_uid == uid else None
    except OSError:
        return None


def runlevel_entry(script: Path) -> bool:
    entry = OPENRC_RUNLEVELS / OPENRC_RUNLEVEL / OPENRC_SERVICE
    if not os.path.lexists(entry):
        return False
    if not entry.is_symlink() or os.readlink(entry) != str(script):
        raise WorkspaceError("Refusing a foreign repairlib entry in the OpenRC runlevel.")
    return True


def find_process(predicate: Callable[[list[str]], bool], uid: int | None = None) -> int | None:
    """Find a process by its exact NUL-separated argv when OpenRC metadata is root-only."""
    try:
        entries = os.scandir(OPENRC_PROC)
    except OSError:
        return None
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if uid is not None and entry.stat(follow_symlinks=False).st_uid != uid:
                    continue
                raw = (OPENRC_PROC / entry.name / "cmdline").read_bytes()[:1 << 16]
            except OSError:
                continue
            argv = [part.decode("utf-8", "surrogateescape") for part in raw.rstrip(b"\0").split(b"\0")]
            if argv and predicate(argv):
                return int(entry.name)
    return None


def openrc_state(script: Path, present: bool, expected: tuple[str, ...] | None = None) -> dict:
    """Unprivileged readback of supervisor state, PIDs and runlevel membership."""
    in_runlevel = runlevel_entry(script)
    if not present:
        if in_runlevel or any(os.path.lexists(OPENRC_SVCDIR / marker / OPENRC_SERVICE)
                              for marker in OPENRC_MARKERS):
            raise WorkspaceError("OpenRC still tracks a repairlib service without its script; refusing.")
        return {"state": "absent", "pid": None, "supervisor_pid": None, "in_runlevel": False}
    result = openrc_command(["rc-service", OPENRC_SERVICE, "status"])
    if (state := OPENRC_EXIT.get(result.returncode)) is None:
        raise SupervisorError("Could not read repairlib state from OpenRC.")
    pid = supervisor = None
    if state != "stopped":
        supervisor = read_pid(OPENRC_PIDFILE, OPENRC_OWNER[0]) or find_process(
            lambda argv: len(argv) > 1 and Path(argv[0]).name == "supervise-daemon"
            and argv[1] == OPENRC_SERVICE, OPENRC_OWNER[0])
        pid = read_pid(OPENRC_SVCDIR / "options" / OPENRC_SERVICE / "child_pid", os.getuid())
        if pid is None and expected is not None:
            pid = find_process(lambda argv: tuple(argv) == expected, os.getuid())
    return {"state": state, "pid": pid, "supervisor_pid": supervisor, "in_runlevel": in_runlevel}


def health_ready(port: int) -> bool:
    """One fresh loopback /health request; True only for a ready, initialized app."""
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/health", headers={"Cache-Control": "no-cache"})
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read(4096)).get("ready") is True
    except (OSError, HTTPException, ValueError, AttributeError):
        return False
    finally:
        connection.close()


def owns_listener(pid: int, port: int) -> bool:
    """Confirm the application process owns a listening socket on the expected port."""
    inodes: set[str] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (Path("/proc/net") / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if (len(fields) > 9 and fields[3] == "0A"
                    and fields[1].endswith(f":{port:04X}")):
                inodes.add(fields[9])
    if not inodes:
        return False
    try:
        descriptors = (OPENRC_PROC / str(pid) / "fd").iterdir()
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            return True
    return False


def await_running(script: Path, port: int, expected: tuple[str, ...]) -> tuple[dict, bool]:
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        current = openrc_state(script, True, expected)
        if (current["state"] == "started" and current["pid"] is not None
                and owns_listener(current["pid"], port) and health_ready(port)):
            # Re-read so the supervisor state is no older than the health response.
            current = openrc_state(script, True, expected)
            if current["state"] == "started":
                return current, True
        if current["state"] == "stopped" or time.monotonic() >= deadline:
            return current, False
        time.sleep(READY_INTERVAL)


def openrc_paths(workspace: Path) -> tuple[Path, Path]:
    if any(which(tool) is None for tool in OPENRC_TOOLS) or not OPENRC_RUN.is_file():
        raise WorkspaceError("OpenRC (rc-service, rc-update, supervise-daemon) is unavailable. "
                             "Use the systemd user service or service foreground.")
    if not OPENRC_SVCDIR.is_dir():
        raise WorkspaceError("OpenRC is installed but is not the running service manager.")
    for directory in (OPENRC_INITD, OPENRC_RUNLEVELS / OPENRC_RUNLEVEL):
        check_path(directory)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != OPENRC_OWNER[0] or info.st_mode & 0o022:
            raise WorkspaceError(f"{directory} is not a root-owned OpenRC directory; refusing.")
    for system in (OPENRC_INITD, OPENRC_RUNLEVELS, OPENRC_SVCDIR, OPENRC_PIDFILE.parent):
        if overlaps(workspace, system):
            raise WorkspaceError("Workspace and OpenRC system paths must be separate.")
    config = check_path(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))
    if overlaps(workspace, config):
        raise WorkspaceError("Workspace and service configuration paths must be separate.")
    if config.exists():
        info = config.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise WorkspaceError("Service configuration directories must be user owned and protected from other writers.")
    return OPENRC_INITD / OPENRC_SERVICE, check_path(config / "repairlib-openrc")


def publish_script(tool: str, script: Path, content: bytes, launcher: tuple[str, ...]) -> Callable[[], None]:
    """Validate in private staging, then link root:root 0755 without clobbering.

    Returns the rollback for the published script; raises before publication otherwise.
    """
    if openrc_command([*launcher, "service", "--help"], timeout=60).returncode:
        raise WorkspaceError("The RepairLib launcher failed validation in this runtime.")
    with TemporaryDirectory(prefix="repairlib-openrc-") as staging:
        staged = Path(staging) / OPENRC_SERVICE
        staged.write_bytes(content)
        if openrc_command(["sh", "-n", str(staged)]).returncode:
            raise WorkspaceError("The generated OpenRC script failed sh -n validation.")
        temporary = script.with_name(f".{OPENRC_SERVICE}.{token_hex(8)}")
        privileged(tool, "install", "-o", "root", "-g", "root", "-m", "0755", str(staged), str(temporary))
        try:
            # link(2) is atomic and never replaces an existing script.
            privileged(tool, "ln", str(temporary), str(script))
        finally:
            with suppress(SupervisorError):
                privileged(tool, "rm", "-f", str(temporary))
    return lambda: privileged(tool, "rm", "-f", str(script))


def manage_openrc(action: str, workspace: Path, port: int, lan: bool = False) -> int:
    script, control = openrc_paths(workspace)
    executable = check_path(Path(sys.executable), canonical=False)
    user, group = current_identity()
    state, version = workspace_schema(workspace)
    quoted = f"--workspace {shlex.quote(str(workspace))} --port {port}" + (" --lan" if lan else "")
    report = {
        "backend": "openrc", "script": str(script), "name": OPENRC_SERVICE,
        "runlevel": OPENRC_RUNLEVEL, "user": f"{user}:{group}", "pidfile": str(OPENRC_PIDFILE),
        "service": "unknown", "workspace": str(workspace), "workspace_state": state,
        "schema_version": version, "expected_schema_version": SCHEMA_VERSION,
        "python": str(executable), "source": str(SOURCE), "url": f"http://127.0.0.1:{port}",
        "pid": None, "logs": "grep repairlib /var/log/messages (syslog via logger)",
        "readback": f"repairlib service status --openrc-system {quoted}",
        "next": setup_hint(state),
    }
    try:
        launchers = service_launchers(executable)
        argv = foreground_args(workspace, port, lan=lan)
        registrations = {openrc_script(launcher + argv, user, group): launcher + argv for launcher in launchers}
        content = next(iter(registrations))
        preferred = registrations[content]
        if action == "install" and state in {"invalid", "unsupported_schema"}:
            raise WorkspaceError(setup_hint(state))
        with ExitStack() as stack:
            # Unprivileged preflight: nothing is written and no privilege is used before this.
            existing = registered_script(script, *registrations)
            present = existing is not None
            command = registrations.get(existing, preferred)
            current = openrc_state(script, present, command)
            mutating = action != "status" and (present or action == "install")
            if action in {"start", "stop", "restart"} and not present:
                raise WorkspaceError("Run service install --openrc-system before controlling this service.")
            if action in {"start", "restart"} and (hint := setup_hint(state)):
                raise WorkspaceError(hint)
            tool = None
            if mutating and not (action == "install" and present and current["in_runlevel"]):
                tool = privilege_tool()
            if mutating:
                private_control(control, create=True)
                stack.enter_context(maintenance_lock(control, exclusive=True))
                existing = registered_script(script, *registrations)
                present = existing is not None
                command = registrations.get(existing, preferred)
                current = openrc_state(script, present, command)
            if action == "install" and tool:
                rollbacks: list[Callable[[], None]] = []
                try:
                    if not present:
                        rollbacks.append(publish_script(tool, script, content, launchers[0]))
                        if not registered_script(script, content):
                            raise SupervisorError("The published OpenRC script could not be verified.")
                    if not runlevel_entry(script):
                        privileged(tool, "rc-update", "add", OPENRC_SERVICE, OPENRC_RUNLEVEL)
                        rollbacks.append(lambda: privileged(
                            tool, "rc-update", "del", OPENRC_SERVICE, OPENRC_RUNLEVEL))
                    current = openrc_state(script, True, command)
                    if not current["in_runlevel"]:
                        raise SupervisorError("OpenRC did not confirm runlevel membership.")
                except (WorkspaceError, OSError):
                    for rollback in reversed(rollbacks):
                        with suppress(WorkspaceError, OSError):
                            rollback()
                    raise
            elif action in {"start", "restart"}:
                if current["state"] != "started" and health_ready(port):
                    raise WorkspaceError(f"Another listener already answers on 127.0.0.1:{port}; "
                                         "stop it before starting the OpenRC service.")
                privileged(tool, "rc-service", OPENRC_SERVICE, action)
            elif action in {"stop", "remove"} and present:
                privileged(tool, "rc-service", OPENRC_SERVICE, "stop")
                current = openrc_state(script, True, command)
                if action == "remove" and current["state"] == "stopped":
                    readd = False
                    try:
                        if current["in_runlevel"]:
                            privileged(tool, "rc-update", "del", OPENRC_SERVICE, OPENRC_RUNLEVEL)
                            readd = True
                        # Re-verify the exact bytes right before deleting the matching script.
                        if existing is None or registered_script(script, existing) is None:
                            raise SupervisorError("The OpenRC script disappeared before removal.")
                        privileged(tool, "rm", str(script))
                    except (WorkspaceError, OSError):
                        if readd and os.path.lexists(script):
                            with suppress(WorkspaceError, OSError):
                                privileged(tool, "rc-update", "add", OPENRC_SERVICE, OPENRC_RUNLEVEL)
                        raise
                    current = openrc_state(script, False, command)

            ready = None
            if action in {"start", "restart"}:
                current, ready = await_running(script, port, command)
            elif current["state"] == "started":
                ready = bool(current["pid"] and owns_listener(current["pid"], port)
                             and health_ready(port))
            observed = {"absent": "absent", "stopped": "stopped"}.get(current["state"], current["state"])
            if current["state"] == "started":
                observed = "running" if ready else "unready"
            report.update(service=observed, supervisor_state=current["state"], pid=current["pid"],
                          supervisor_pid=current["supervisor_pid"], in_runlevel=current["in_runlevel"],
                          health=None if ready is None else ("ready" if ready else "unavailable"))
            if report["next"] is None and observed in {"stopped", "absent"} and action != "remove":
                verb = "install" if observed == "absent" else "start"
                report["next"] = f"repairlib service {verb} --openrc-system {quoted}"
            expected = {"start": "running", "restart": "running", "stop": "stopped", "remove": "absent"}
            if action in expected and observed != expected[action]:
                if action in {"start", "restart"}:
                    raise SupervisorError(f"RepairLib was not started and healthy within {READY_TIMEOUT:g}s. "
                                          "Check the logs and rc-service repairlib status.")
                raise SupervisorError("OpenRC did not confirm the requested state.")
    except WorkspaceError as error:
        report["error"] = str(error)
        if isinstance(error, UnmanagedScript) and error.preimage:
            report["existing_sha256"] = error.preimage
        if isinstance(error, SupervisorError) and report["service"] == "unknown":
            report["next"] = "If OpenRC is unavailable, use service foreground after setup."
        print(json.dumps(report))
        return 1
    print(json.dumps(report))
    return 0


def using_openrc() -> bool:
    """True when OpenRC is the running service manager rather than systemd."""
    return (not Path("/run/systemd/system").is_dir() and OPENRC_RUN.is_file()
            and which("rc-service") is not None)


def ignored_in_checkout(workspace: Path, checkout: Path) -> bool:
    """True when the checkout's own Git ignores this workspace path."""
    try:
        done = subprocess.run(["git", "-C", str(checkout), "check-ignore", "-q", str(workspace)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return done.returncode == 0


def run_service(action: str, workspace: Path, port: int, *, openrc_system: bool = False,
                lan: bool = False) -> int:
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise WorkspaceError("Service commands require an unprivileged Linux user.")
        if openrc_system and action == "foreground":
            raise WorkspaceError("--openrc-system manages a supervised service; foreground runs unsupervised.")
        openrc_system = openrc_system or using_openrc()
        if not 1 <= port <= 65535:
            raise WorkspaceError("Choose a fixed loopback port from 1 to 65535.")
        check_path(workspace)
        check_path(SOURCE)
        if overlaps(workspace, Path(sys.prefix).resolve()):
            raise WorkspaceError("Workspace and runtime paths must be separate.")
        if overlaps(workspace, SOURCE.parent) and not ignored_in_checkout(workspace, SOURCE.parent):
            raise WorkspaceError("A workspace inside the checkout must be Git-ignored; add it to .gitignore.")
        if action == "foreground":
            return foreground(workspace, port, lan=lan)
        if openrc_system:
            return manage_openrc(action, workspace, port, lan)
        return manage(action, workspace, port, lan)
    except (WorkspaceError, OSError, RuntimeError) as error:
        message = str(error) if isinstance(error, WorkspaceError) else "Could not safely access service or workspace paths."
        print(f"service {action}: {message}", file=sys.stderr)
        return 1
