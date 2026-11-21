#!/usr/bin/env python3
"""Install RepairLib or rehearse a forward update from one local Git commit."""

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile

OPENRC_SERVICE = Path("/etc/init.d/repairlib")
# Stock distribution Python and distribution packages only: no pip, venv, uv or downloads.
SYSTEM_PYTHON = Path("/usr/bin/python3")
MINIMUM_PYTHON = (3, 13)
REQUIRED_MODULES = ("fastapi", "jinja2", "uvicorn", "pydantic", "httpx", "argon2", "python_multipart")


class InstallError(Exception):
    """The installer refused or could not finish provisioning."""


def host_plan() -> dict:
    """Describe the interpreter this host will run; refuse anything but the stock Python."""
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise InstallError(f"Unsupported architecture: {platform.machine() or 'unknown'}.")
    version = platform.python_version()
    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise InstallError(f"RepairLib needs system Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer; "
                           f"this is {version}.")
    executable = Path(os.path.realpath(sys.executable))
    if executable.parent != SYSTEM_PYTHON.resolve().parent or not SYSTEM_PYTHON.is_file():
        raise InstallError(f"Run the installer with the distribution's {SYSTEM_PYTHON}; "
                           f"this interpreter is {executable}.")
    return {"python": {"mode": "system", "version": version, "executable": str(SYSTEM_PYTHON)}}


def git(repo: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.check_output(["git", "--no-replace-objects", "-C", repo, *args], env=env)


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def runtime_command(generation: Path, code: str) -> list[str]:
    return [str(SYSTEM_PYTHON), "-I", "-B", "-c",
            "import sys; sys.path.insert(0, sys.argv.pop(1)); " + code,
            str(generation / "source/src")]


def entrypoint(generation: Path) -> list[str]:
    return runtime_command(generation, "import runpy; runpy.run_module('repairlib', run_name='__main__')")


def provision(generation: Path) -> None:
    """Verify the staged source runs on the installed distribution packages."""
    missing = subprocess.run(
        [str(SYSTEM_PYTHON), "-I", "-c",
         "import importlib.util, sys; "
         "print(' '.join(n for n in sys.argv[1:] if importlib.util.find_spec(n) is None))",
         *REQUIRED_MODULES], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    ).stdout.split()
    if missing:
        raise InstallError("Install the distribution packages that provide these Python modules: "
                           + ", ".join(missing) + ".")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "CONDA_"))}
    subprocess.run([*entrypoint(generation), "--help"], cwd=generation, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_path(path: Path, repo: Path) -> None:
    if (not path.is_absolute() or ".." in path.parts or not str(path).isprintable()
            or path.resolve() != path or overlaps(path, repo)):
        raise InstallError("Choose absolute paths separate from the source checkout, with no links or traversal.")


def validate_target(target: Path, repo: Path) -> None:
    validate_path(target, repo)
    if target.exists():
        raise InstallError("The install target already exists. Choose a fresh path.")
    if not target.parent.is_dir():
        raise InstallError("The target parent must already exist.")
    parent = target.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise InstallError("The target parent must be user owned and protected from other writers.")


def resolve_commit(repo: Path, revision: str) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}").decode().strip()


def verify_installer(script: Path, repo: Path, commit: str) -> None:
    if script.read_bytes() != git(repo, "show", f"{commit}:scripts/install.py"):
        raise InstallError("The running installer must match scripts/install.py at the selected commit.")


def reserve_target(target: Path) -> None:
    # Build at the final absolute path; a successful exclusive mkdir reserves it.
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as error:
        raise InstallError("The install target already exists. Choose a fresh path.") from error


def stage_runtime(repo: Path, commit: str, target: Path) -> dict:
    plan = host_plan()
    generation = target / "generation"
    source = generation / "source"
    source.mkdir(parents=True)
    archive = git(repo, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        if tree.pax_headers.get("comment") != commit:
            raise InstallError("The staged source archive does not identify the selected commit.")
        tree.extractall(source, filter="data")
    script = source / "scripts/install.py"
    verify_installer(script, repo, commit)
    provision(generation)
    verify_installer(script, repo, commit)
    return {"commit": commit, "python": plan["python"], "launcher": str(target / "repairlib")}


def write_report(path: Path, report: dict) -> None:
    with path.open("x", encoding="utf-8") as file:
        file.write(json.dumps(report, indent=2) + "\n")


SELECTED_BOOT = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from repairlib.lifecycle.update import launch_selected; raise SystemExit(launch_selected())"
)
ORIGINAL_MANAGED_BOOT = """\
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from repairlib.__main__ import main
from repairlib.lifecycle.workspace import WorkspaceError, maintenance_lock
root, _, _, _, workspace, _, port = sys.argv[1:]
try:
    with maintenance_lock(Path(workspace), exclusive=False, create=False):
        if os.path.lexists(Path(root) / "current.json"):
            sys.exit("RepairLib update selection changed. Restart through the user manager.")
        sys.exit(main(["service", "foreground", "--workspace", workspace, "--port", port]))
except (OSError, WorkspaceError):
    sys.exit("RepairLib could not lock the original workspace.")
"""

LAUNCH = """\
import fcntl, json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
workspace = sys.argv[2]
args = sys.argv[3:]
generation = root / "generation"
boot = ("import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
        "runpy.run_module('repairlib', run_name='__main__')")
try:
    managed = (not workspace and len(args) == 6 and
               args[:3] == ["service", "managed-foreground", "--workspace"] and args[4] == "--port")
    # The installer holds the root lock while systemd waits for readiness.
    # Managed startup takes the workspace lock and rechecks the selection there.
    if not managed:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
    descriptor = root / "current.json"
    if os.path.lexists(descriptor):
        if descriptor.is_symlink() or not descriptor.is_file():
            sys.exit("RepairLib update selection is invalid. Run installer recovery.")
        selection = json.loads(descriptor.read_bytes())
        if (selection["format"] != 1 or selection["state"] not in ("ready", "reopening", "open")
                or workspace):
            sys.exit("RepairLib update is interrupted. Run installer recovery.")
        workspace = selection["stage"]["candidate_workspace"]
        selected = Path(workspace)
        if not selected.is_absolute() or selected.name != "workspace":
            sys.exit("RepairLib update selection is invalid. Run installer recovery.")
        generation = selected.parent / "generation"
        boot = @SELECTED_BOOT@
        # The selected listener owns a workspace lock. It must let the installer
        # take the installation lock while it waits at the HTTP write boundary.
        if not managed:
            os.set_inheritable(fd, args[:2] != ["service", "foreground"])
        args = [str(root), workspace, *args]
    elif managed:
        boot = @ORIGINAL_MANAGED_BOOT@
        args = [str(root), *args]
    elif workspace:
        command = args[:1]
        options = args[1:]
        if options[:2] == ["--workspace", workspace]:
            options = options[2:]
        elif options[-2:] == ["--workspace", workspace]:
            options = options[:-2]
        if not (command == ["doctor"] and not options
                or command == ["status"] and options in ([], ["--json"])):
            sys.exit("Candidate writes are closed. Use doctor or status.")
        args = command + options + ["--workspace", workspace]
    executable = "/usr/bin/python3"
    os.execv(executable, [executable, "-I", "-B", "-c", boot,
                          str(generation / "source/src"), *args])
except (OSError, ValueError, TypeError, KeyError):
    sys.exit("RepairLib launcher could not read the selection or acquire the installation lock.")
""".replace("@SELECTED_BOOT@", repr(SELECTED_BOOT)).replace(
    "@ORIGINAL_MANAGED_BOOT@", repr(ORIGINAL_MANAGED_BOOT),
)


def launcher_contents(target: Path, workspace: Path | None = None, *, code: str = LAUNCH) -> str:
    command = [str(SYSTEM_PYTHON), "-I", "-B", "-c", code,
               str(target), str(workspace) if workspace is not None else ""]
    return "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n'


def publish_launcher(target: Path, report: dict, workspace: Path | None = None) -> None:
    write_report(target / "install.json", report)
    launcher = target / "repairlib"
    launcher.write_text(launcher_contents(target, workspace), encoding="utf-8")
    launcher.chmod(0o700)


def install(script: Path, revision: str, target: Path) -> dict:
    repo = script.parent.parent
    validate_target(target, repo)
    commit = resolve_commit(repo, revision)
    verify_installer(script, repo, commit)
    reserve_target(target)
    try:
        report = stage_runtime(repo, commit, target)
        publish_launcher(target, report)
        return report
    except BaseException:
        shutil.rmtree(target)
        raise


def installed_commit(repo: Path, target: Path) -> str:
    try:
        report = json.loads((target / "install.json").read_bytes())
        commit = report["commit"]
        if len(commit) not in (40, 64) or any(char not in "0123456789abcdef" for char in commit):
            raise ValueError
        generation_source = target / "generation/source"
        if (report["launcher"] != str(target / "repairlib")
                or (generation_source / "scripts/install.py").read_bytes()
                != git(repo, "show", f"{commit}:scripts/install.py")
                or not (target / "repairlib").is_file()):
            raise ValueError
    except (OSError, TypeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise InstallError("Existing installation metadata does not match the local Git history.") from error
    return commit


def private_directory(path: Path, repo: Path) -> None:
    validate_path(path, repo)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise InstallError("The installation and workspace must be private directories owned by you.")


@contextmanager
def installation_lock(target: Path):
    import fcntl

    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError("The installation lock is already held.") from error
        yield fd
    finally:
        os.close(fd)


def generation_fingerprint(installation: Path) -> str:
    generation = installation / "generation"
    if not stat.S_ISDIR(generation.lstat().st_mode):
        raise InstallError("The installed generation directory changed.")
    digest = hashlib.sha256()
    for directory, names, files in os.walk(generation, followlinks=False):
        names.sort()
        for name in sorted(names + files):
            path = Path(directory) / name
            info = path.lstat()
            relative = os.fsencode(path.relative_to(generation))
            digest.update(f"{len(relative)}:{info.st_mode}:".encode() + relative)
            if stat.S_ISLNK(info.st_mode):
                digest.update(hashlib.sha256(os.fsencode(os.readlink(path))).digest())
            elif stat.S_ISREG(info.st_mode):
                digest.update(bytes.fromhex(sha256(path)))
            elif not stat.S_ISDIR(info.st_mode):
                raise InstallError("The generation contains an unexpected file type.")
    return digest.hexdigest()


def update_worker(candidate: Path, *args: str, request: str, fd: int) -> dict:
    command = runtime_command(candidate / "generation",
                              "import runpy; runpy.run_module('repairlib.lifecycle.update', run_name='__main__')")
    result = subprocess.run([*command, *args], cwd=candidate, env=os.environ | {"TMPDIR": str(candidate)},
                            input=request, text=True, capture_output=True, pass_fds=(fd,))
    if result.returncode != 0:
        # The worker emits only fixed diagnostics. Abrupt exits leave the last descriptor.
        reason = result.stdout.strip()
        raise InstallError(reason or "Update worker interrupted. Check the selection and run recovery.")
    return json.loads(result.stdout)


def refuse_openrc_service() -> None:
    if os.path.lexists(OPENRC_SERVICE):
        raise InstallError(
            "The OpenRC repairlib service is installed. Remove it with "
            "`repairlib service remove --openrc-system` before staging or activating an "
            "update; OpenRC managed handoff is not supported yet."
        )


def verify_forward_update(script: Path, repo: Path, previous: str, commit: str) -> None:
    if commit == previous:
        raise InstallError("The requested commit is the same as the installed commit.")
    try:
        git(repo, "merge-base", "--is-ancestor", previous, commit)
    except subprocess.CalledProcessError as error:
        raise InstallError("The requested commit must be a descendant of the installed commit.") from error
    verify_installer(script, repo, commit)


def check_update(script: Path, revision: str, target: Path) -> dict:
    repo = script.parent.parent
    report = dict(installed_commit=None, selected_commit=None, requested_commit=None, phase=None, blocker=None)
    try:
        private_directory(target, repo)
        previous = report["installed_commit"] = installed_commit(repo, target)
        report["phase"] = "original"
        descriptor = target / "current.json"
        if os.path.lexists(descriptor):
            report["phase"] = None
            try:
                if not stat.S_ISREG(descriptor.lstat().st_mode):
                    raise ValueError
                current = json.loads(descriptor.read_bytes())
                phase, stage = current["state"], current["stage"]
                selected = stage["candidate_commit"]
                if (current["format"] != 1 or phase not in ("pending", "selected", "ready", "reopening", "open")
                        or stage["source_install"] != str(target)
                        or len(selected) not in (40, 64) or any(c not in "0123456789abcdef" for c in selected)):
                    raise ValueError
            except (OSError, KeyError, TypeError, ValueError) as error:
                raise InstallError("The update selection is malformed or unreadable. Check recovery guidance.") from error
            report.update(phase=phase, selected_commit=selected)
        try:
            commit = report["requested_commit"] = resolve_commit(repo, revision)
        except subprocess.CalledProcessError as error:
            raise InstallError("The requested commit is not available in local Git history.") from error
        refuse_openrc_service()
        if report["selected_commit"] is not None:
            if report["phase"] != "open":
                raise InstallError("An update selection already exists. Finish recovery before staging again.")
            if "managed" in current:
                raise InstallError("An open managed update selection already exists. Repeated staging is foreground only.")
            workspace = Path(stage.get("candidate_workspace", ""))
            private_directory(workspace, repo)
            source = open_source(repo, target, current, workspace, target)
            previous = installed_commit(repo, source)
        verify_forward_update(script, repo, previous, commit)
    except (OSError, InstallError) as error:
        report["blocker"] = str(error)
    return report


def open_source(repo: Path, target: Path, selection: dict, workspace: Path, candidate: Path) -> Path:
    if selection.get("format") != 1 or selection.get("state") != "open" or "managed" in selection:
        raise InstallError("An update selection already exists. Repeated staging requires an open foreground selection.")
    stage = selection["stage"]
    if (stage["source_install"] != str(target) or workspace.name != "workspace"
            or stage["candidate_workspace"] != str(workspace)):
        raise InstallError("Use the currently selected workspace as the update source.")
    source = workspace.parent
    private_directory(source, repo)
    if (overlaps(source, target) or overlaps(source, candidate)
            or installed_commit(repo, source) != stage["candidate_commit"]):
        raise InstallError("The selected generation does not match the recorded commit or candidate path.")
    return source


def stage_update(script: Path, revision: str, target: Path, workspace: Path, candidate: Path) -> dict:
    refuse_openrc_service()
    repo = script.parent.parent
    validate_target(candidate, repo)
    for path in (target, workspace):
        private_directory(path, repo)
    if overlaps(target, workspace) or any(overlaps(candidate, path) for path in (target, workspace)):
        raise InstallError("Installation, workspace and candidate must be separate.")

    with installation_lock(target) as fd:
        descriptor = target / "current.json"
        prior = None
        source = target
        if os.path.lexists(descriptor):
            if not stat.S_ISREG(descriptor.lstat().st_mode):
                raise InstallError("The update selection is not a regular file.")
            prior = descriptor.read_bytes()
            source = open_source(repo, target, json.loads(prior), workspace, candidate)
        previous = installed_commit(repo, source)
        commit = resolve_commit(repo, revision)
        verify_forward_update(script, repo, previous, commit)

        migration_probe = (
            "import hashlib, json; "
            "from repairlib.storage.initialization import MIGRATIONS, migration_content; "
            "print(json.dumps([hashlib.sha256(migration_content(i)).hexdigest() "
            "for i in range(1, len(MIGRATIONS) + 1)]))"
        )
        history = subprocess.check_output(runtime_command(source / "generation", migration_probe),
                                          cwd=source, stdin=subprocess.DEVNULL, text=True, timeout=30)
        reserve_target(candidate)
        try:
            install_report = stage_runtime(repo, commit, candidate)
            rehearsal = update_worker(candidate, str(workspace), str(candidate), request=history, fd=fd)
            supports_repeat = rehearsal.pop("repeated_update_worker", False)
            report = {
                "format": 2,
                "state": "staged",
                "source_commit": previous,
                "source_install": str(target),
                "source_workspace": str(workspace),
                "candidate_commit": commit,
                "candidate_workspace": str(candidate / "workspace"),
                "python": install_report["python"],
                "source_generation_fingerprint": generation_fingerprint(source),
                "candidate_generation_fingerprint": generation_fingerprint(candidate),
                "backup": str(candidate / "backup.zip"),
                "backup_sha256": sha256(candidate / "backup.zip"),
                **rehearsal,
            }
            if prior is not None:
                if supports_repeat is not True:
                    raise InstallError("The candidate runtime does not support repeated update recovery.")
                (candidate / "previous-selection.json").write_bytes(prior)
                report["previous_selection_sha256"] = hashlib.sha256(prior).hexdigest()
            publish_launcher(candidate, install_report, candidate / "workspace")
            pending = candidate / ".stage-pending.json"
            write_report(pending, report)
            pending.rename(candidate / "stage.json")
            return report
        except BaseException:
            if not (candidate / "backup.zip").exists():
                shutil.rmtree(candidate)
            raise


def change_selection(action: str, script: Path, target: Path, candidate: Path, *,
                     port: int | None = None, managed: bool = False) -> dict:
    refuse_openrc_service()
    repo = script.parent.parent
    for path in (target, candidate):
        private_directory(path, repo)
    if overlaps(target, candidate):
        raise InstallError("Installation and candidate must be separate.")
    with installation_lock(target) as fd:
        descriptor = target / "current.json"
        selected = os.path.lexists(descriptor)
        if action == "reopen" and not selected:
            raise InstallError("Activate the update before handing off the candidate listener.")
        if selected and not stat.S_ISREG(descriptor.lstat().st_mode):
            raise InstallError("The update selection is not a regular file.")

        receipt = candidate / "stage.json"
        if not os.path.lexists(receipt) or not stat.S_ISREG(receipt.lstat().st_mode):
            raise InstallError("There is no completed staged record. Stage a fresh candidate.")
        stage = json.loads(receipt.read_bytes())
        if (not isinstance(stage, dict) or stage.get("format") != 2 or stage.get("state") != "staged"
                or stage.get("source_install") != str(target)
                or stage.get("candidate_workspace") != str(candidate / "workspace")
                or stage.get("backup") != str(candidate / "backup.zip")):
            raise InstallError("The staged record does not match this installation. Stage a fresh candidate.")
        workspace = Path(stage["source_workspace"])
        for path in (workspace, candidate / "workspace"):
            private_directory(path, repo)
        if overlaps(workspace, target) or overlaps(workspace, candidate):
            raise InstallError("Installation, workspace and candidate must be separate.")
        source = target
        prior = None
        if "previous_selection_sha256" in stage:
            if managed:
                raise InstallError("Repeated updates are supported only for foreground installations.")
            saved = candidate / "previous-selection.json"
            if not stat.S_ISREG(saved.lstat().st_mode):
                raise InstallError("The saved previous selection is not a regular file.")
            prior = saved.read_bytes()
            if hashlib.sha256(prior).hexdigest() != stage["previous_selection_sha256"]:
                raise InstallError("The saved previous selection changed since staging.")
            source = open_source(repo, target, json.loads(prior), workspace, candidate)
        original = installed_commit(repo, source)
        commit = stage["candidate_commit"]
        verify_installer(script, repo, commit)
        if action == "activate":
            if (descriptor.read_bytes() if selected else None) != prior:
                raise InstallError("The update selection already exists or changed since staging.")
        elif prior is not None and selected and descriptor.read_bytes() == prior and action == "recover":
            return {"state": "open", "commit": original}
        elif selected:
            current = json.loads(descriptor.read_bytes())
            if (not isinstance(current, dict) or current.get("format") != 1
                    or current.get("state") not in {"pending", "selected", "ready", "reopening", "open"}
                    or current.get("stage") != stage):
                raise InstallError("The update selection changed. Reconciliation requires the recorded candidate.")
            if ("managed" in current) != managed:
                raise InstallError("Use the same managed or foreground mode as activation.")
        elif action == "recover" and not managed and prior is None:
            if original != stage["source_commit"]:
                raise InstallError("The original installation changed since staging.")
            return {"state": "original", "commit": original}
        backup = candidate / "backup.zip"
        if (original != stage["source_commit"] or installed_commit(repo, candidate) != commit
                or generation_fingerprint(source) != stage["source_generation_fingerprint"]
                or generation_fingerprint(candidate) != stage["candidate_generation_fingerprint"]
                or not stat.S_ISREG(backup.lstat().st_mode) or sha256(backup) != stage["backup_sha256"]):
            raise InstallError("The installation, candidate generation or backup changed since staging.")
        if (candidate / "repairlib").read_text() != launcher_contents(candidate, candidate / "workspace"):
            raise InstallError("The candidate launcher changed since staging.")

        stable = launcher_contents(target)
        original_launcher = (target / "repairlib").read_text()
        if original_launcher != stable:
            raise InstallError("The original launcher changed since staging.")
        request = {"stage": stage, "launcher": None, "port": port,
                   "previous_selection": prior.decode("utf-8") if prior is not None else None,
                   "managed_boots": [ORIGINAL_MANAGED_BOOT, SELECTED_BOOT] if managed else None}
        return update_worker(candidate, action, str(target), request=json.dumps(request), fd=fd)


def activate_update(script: Path, target: Path, candidate: Path) -> dict:
    return change_selection("activate", script, target, candidate)


def recover_update(script: Path, target: Path, candidate: Path, **options) -> dict:
    try:
        return change_selection("recover", script, target, candidate, **options)
    except (InstallError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        # Preflight can fail before it reaches the worker (for example if the
        # retained archive is missing). Identify an open selection for guidance.
        descriptor = target / "current.json"
        try:
            open_selection = (stat.S_ISREG(descriptor.lstat().st_mode)
                              and json.loads(descriptor.read_bytes())["state"] == "open")
        except (OSError, KeyError, TypeError, ValueError):
            open_selection = False
        if open_selection:
            reason = str(error) if isinstance(error, InstallError) else "Could not verify the open update."
            raise InstallError(f"{reason} Reconciliation must preserve the candidate workspace.") from error
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD", help="local revision to install (default: HEAD)")
    parser.add_argument("--target", required=True, type=Path, help="absolute path to the installation")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-update", action="store_true", help="report local update blockers without staging")
    modes.add_argument("--stage-update", action="store_true", help="stage against a stopped disposable installation")
    modes.add_argument("--activate-update", action="store_true", help="select the candidate with writes closed")
    modes.add_argument("--reopen-update", action="store_true", help="verify the candidate listener and open writes")
    modes.add_argument("--recover-update", action="store_true", help="recover before writes or verify the open candidate")
    parser.add_argument("--managed", action="store_true", help="hand off the existing Linux user unit")
    parser.add_argument("--port", type=int, help="fixed loopback port for reopening or managed handoff")
    parser.add_argument("--workspace", type=Path, help="existing disposable workspace for update staging")
    parser.add_argument("--candidate", type=Path, help="absolute path of the disposable candidate")
    args = parser.parse_args()
    if bool(args.workspace) != args.stage_update:
        parser.error("--workspace is required only for --stage-update")
    if bool(args.candidate) != (args.stage_update or args.activate_update or args.reopen_update or args.recover_update):
        parser.error("--candidate is required for update operations")
    if args.managed and not (args.activate_update or args.reopen_update or args.recover_update):
        parser.error("--managed requires activation, reopening or recovery")
    if args.reopen_update or args.managed:
        if args.port is None or not 1 <= args.port <= 65535:
            parser.error("Reopening and managed handoff require a listener --port from 1 to 65535")
    elif args.port is not None:
        parser.error("--port requires reopening or managed handoff")
    if sys.version_info < MINIMUM_PYTHON:
        parser.exit(1, "install: the bootstrap requires Python 3.13 or newer.\n")
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.exit(1, "install: requires unprivileged Linux.\n")
    try:
        host_plan()
        script = Path(__file__).resolve()
        if args.check_update:
            report = check_update(script, args.commit, args.target)
        elif args.stage_update:
            report = stage_update(script, args.commit, args.target, args.workspace, args.candidate)
        elif args.activate_update or args.reopen_update:
            action = "activate" if args.activate_update else "reopen"
            report = change_selection(action, script, args.target, args.candidate,
                                      port=args.port, managed=args.managed)
        elif args.recover_update:
            report = recover_update(script, args.target, args.candidate, port=args.port, managed=args.managed)
        else:
            report = install(script, args.commit, args.target)
    except InstallError as error:
        parser.exit(1, f"install: {error}\n")
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        parser.exit(1, f"install: provisioning failed ({type(error).__name__}). Check Git and target access.\n")
    print(json.dumps(report, indent=2))
    return int(args.check_update and report["blocker"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
