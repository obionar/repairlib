"""Private candidate checks run by the matching installer in the candidate runtime."""

import json
import os
from hashlib import file_digest, sha256
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from secrets import token_hex, token_urlsafe
import shutil
import socket
import stat
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import time

from fastapi.testclient import TestClient

from repairlib.app import create_app
from repairlib.config import DISK_RESERVE_BYTES
from repairlib.doctor import diagnose_workspace
from repairlib.domain import sessions, tokens
from repairlib.domain.setup import initialize_demo_workspace
from repairlib.lifecycle.backup_archive import archived_workspace, private_file, restore_archive, verify_backup
from repairlib.lifecycle.operations import local_status
from repairlib.lifecycle.service import (
    BOOT, UNIT, apply_systemctl, foreground_args, private_control, python_args, read_unit,
    registered_unit, service_paths, unit_bytes,
)
from repairlib.lifecycle.workspace import ACTIVE, LOCK, WorkspaceError, maintenance_lock, migrate_workspace
from repairlib.storage.initialization import SCHEMA_VERSION, migration_content


def check_upgrade(history: list[str]) -> None:
    if not history or len(history) > SCHEMA_VERSION or history != [
            sha256(migration_content(number)).hexdigest() for number in range(1, len(history) + 1)]:
        raise WorkspaceError("Candidate must preserve all installed migrations and their order.")


def check_space(workspace: Path, candidate: Path) -> None:
    # Reserve headroom for the stored archive, one restore, and SQLite's
    # migration/diagnostics scratch space. Rehearsal and candidate restores run in series.
    payload = 0
    for directory, _, names in os.walk(workspace / ACTIVE, followlinks=False):
        for name in names:
            info = (Path(directory) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                payload += info.st_size
    if shutil.disk_usage(candidate).free < DISK_RESERVE_BYTES + 3 * payload:
        raise WorkspaceError("Insufficient free space for backup, restore, candidate checks and the disk reserve.")


def check_health(workspace: Path) -> None:
    # The worker supervises the ASGI lifespan without opening a listener.
    with TestClient(create_app(workspace=workspace, require_initialized=True),
                    base_url="http://localhost") as client:
        response = client.get("/health")
        if response.status_code != 200 or response.json().get("ready") is not True:
            raise WorkspaceError("Private candidate health check failed.")


def workspace_fingerprint(workspace: Path) -> str:
    """Fingerprint stopped state without opening SQLite or changing its sidecars."""
    digest = sha256()
    for path in (workspace, workspace / LOCK):
        info = path.lstat()
        digest.update(f"{info.st_dev}:{info.st_ino}\n".encode())

    def visit(path: Path) -> None:
        info = path.lstat()
        name = os.fsencode(path.relative_to(workspace))
        digest.update(f"{len(name)}:{info.st_mode}:".encode() + name)
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            with path.open("rb") as file:
                digest.update(file_digest(file, "sha256").digest())
        else:
            raise WorkspaceError("Workspace fingerprint requires real directories and private files.")

    visit(workspace / ACTIVE)
    return digest.hexdigest()


def publish_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as file:
        pending = Path(file.name)
        try:
            os.fchmod(file.fileno(), mode)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def listener_identity(stage: dict) -> str:
    return sha256(json.dumps(stage, sort_keys=True).encode()).hexdigest()


def read_selection(descriptor: Path) -> dict:
    with private_file(descriptor) as file:
        selection = json.load(file)
    if not isinstance(selection, dict):
        raise ValueError("Invalid update selection.")
    return selection


def launch_selected() -> int:
    from repairlib.__main__ import main
    from repairlib.lifecycle.service import foreground

    target, workspace, *args = sys.argv[1:]
    descriptor = Path(target) / "current.json"
    try:
        selection = read_selection(descriptor)
        stage = selection["stage"]
        if stage["candidate_workspace"] != workspace:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, WorkspaceError):
        raise SystemExit("RepairLib update selection changed. Run installer recovery.") from None

    def listener_state() -> dict:
        phase = "unavailable"
        try:
            current = read_selection(descriptor)
            state = current["state"]
            if state in ("ready", "reopening", "open") and current == (selection | {"state": state}):
                phase = state
        except (OSError, ValueError, TypeError, KeyError, WorkspaceError):
            pass
        return {"update_phase": phase, "writes_open": phase == "open"}

    state = listener_state()
    if state["update_phase"] == "unavailable":
        raise SystemExit("RepairLib update selection changed. Run installer recovery.")
    command, options = args[:1], args[1:]
    if options[:2] == ["--workspace", workspace]:
        options = options[2:]
    elif options[-2:] == ["--workspace", workspace]:
        options = options[:-2]
    if ((command == ["doctor"] and not options)
            or (command == ["status"] and options in ([], ["--json"]))):
        return main([*command, *options, "--workspace", workspace], update_state=state)
    # Once writes are open the selected generation is the live installation, so ordinary
    # commands run against its workspace; service control stays on the documented paths.
    if state["update_phase"] == "open" and command != ["service"]:
        return main([*command, *options, "--workspace", workspace], update_state=state)
    if args[:2] == ["service", "managed-foreground"]:
        port = selection.get("managed", {}).get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535 or tuple(args) != foreground_args(
                Path(stage["source_workspace"]), port, managed=True):
            raise SystemExit("RepairLib managed listener does not match the selected update.")

        def managed_state() -> dict:
            return listener_state() | {"listener_pid": os.getpid()}

        try:
            with maintenance_lock(Path(workspace), exclusive=False, create=False):
                if listener_state()["update_phase"] == "unavailable":
                    raise WorkspaceError("RepairLib update selection changed. Restart through the user manager.")
                return foreground(Path(workspace), port, listener_state=managed_state,
                                  listener_id=listener_identity(stage))
        except WorkspaceError as error:
            raise SystemExit(str(error)) from None
    if args[:2] == ["service", "foreground"] and len(args) == 4 and args[2] == "--port":
        try:
            port = int(args[3])
            if 1 <= port <= 65535:
                return foreground(Path(workspace), port, listener_state=listener_state,
                                  listener_id=listener_identity(stage))
        except ValueError:
            pass
        except WorkspaceError as error:
            raise SystemExit(str(error)) from None
    raise SystemExit("Use status, doctor, or service foreground --port PORT for the selected update.")


def check_listener(port: int, stage: dict, phase: str | None, pid: int | None = None) -> None:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        health = json.loads(response.read(4096))
        if response.status == 200 and isinstance(health, dict) and health.get("ready") is True:
            if (health.get("update_phase") == phase
                    and (phase is None or (
                        health.get("writes_open") is (phase == "open")
                        and health.get("listener_id") == listener_identity(stage)
                        and (pid is None or health.get("listener_pid") == pid)
                    ))):
                return
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    if phase in {"ready", "reopening"}:
        raise WorkspaceError("Could not verify the selected candidate listener. Writes remain closed.")
    raise WorkspaceError("Could not verify the managed listener after recovery.")


class ManagedListener:
    def __init__(self, target: Path, stage: dict, port: int, boots: list[str]):
        old = Path(stage["source_workspace"])
        clone = Path(stage["candidate_workspace"])
        self.unit, self.control = service_paths(old)
        self.binding = {"unit": str(self.unit), "port": port}
        self.port = port
        self.stage = stage
        self.fixed_argv = self.runtime_argv(target, BOOT) + foreground_args(old, port)
        args = foreground_args(old, port, managed=True)
        self.original_argv = self.runtime_argv(target, boots[0]) + (str(target), *args)
        self.selected_argv = self.runtime_argv(clone.parent, boots[1]) + (str(target), str(clone), *args)
        self.fixed = unit_bytes(self.fixed_argv)
        self.stable = unit_bytes((str(target / "repairlib"), *args))

    @staticmethod
    def runtime_argv(root: Path, boot: str) -> tuple[str, ...]:
        generation = root / "generation"
        return python_args(Path("/usr/bin/python3"), generation / "source/src", boot)

    def read(self, phase: str | None) -> tuple[bytes, bool, int]:
        content = registered_unit(self.unit, self.fixed, self.stable)
        if content is None:
            raise WorkspaceError("Register the existing RepairLib service before managed handoff.")
        if phase is not None and content != self.stable:
            raise WorkspaceError("The managed selection requires its stable launcher unit.")
        current = read_unit(self.unit, True, allow_reload=True)
        pid = int(current["MainPID"])
        stopped = not pid and current["ActiveState"] in {"inactive", "failed"}
        reload_needed = current["LoadState"] != "loaded" or current["NeedDaemonReload"] != "no"
        if reload_needed and (not stopped or content != self.stable):
            raise WorkspaceError("The user manager and unit file disagree. Check the registered unit.")
        if stopped:
            return content, reload_needed, 0
        if not (pid and current["ActiveState"] == "active" and current["SubState"] == "running"):
            raise WorkspaceError("The user manager did not confirm a stopped or running listener.")
        if phase is None:
            expected = self.fixed_argv if content == self.fixed else self.original_argv
        else:
            expected = self.selected_argv if phase in {"ready", "reopening", "open"} else ()
        try:
            actual = tuple((Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")[:-1])
        except OSError:
            actual = ()
        if not expected or actual != tuple(map(os.fsencode, expected)):
            raise WorkspaceError("Refusing an unrelated process reported by the user manager.")
        return content, False, pid

    def stop(self, phase: str | None) -> None:
        if self.read(phase)[2]:
            apply_systemctl("stop", UNIT)
            if self.read(phase)[2]:
                raise WorkspaceError("The user manager did not confirm that the listener stopped.")

    def require_free_port(self) -> None:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                pass
        except ConnectionRefusedError:
            return
        raise WorkspaceError("Refusing an unrelated listener on the managed loopback port.")

    def reload(self, phase: str | None) -> None:
        apply_systemctl("daemon-reload")
        if self.read(phase)[1]:
            raise WorkspaceError("The user manager did not load the stable listener unit.")

    def adopt(self) -> None:
        content, reload_needed, pid = self.read(None)
        if pid:
            raise WorkspaceError("Stop the original listener before replacing its unit.")
        if content == self.fixed:
            publish_bytes(self.unit, self.stable)
            reload_needed = True
        if reload_needed:
            self.reload(None)

    def start(self, phase: str | None) -> int:
        _, reload_needed, pid = self.read(phase)
        if not pid:
            self.require_free_port()
            if reload_needed:
                self.reload(phase)
            apply_systemctl("start", UNIT)
            _, _, pid = self.read(phase)
            if not pid:
                raise WorkspaceError("The user manager did not confirm that the listener started.")
        check_listener(self.port, self.stage, phase, pid)
        return pid


def update_selection(action: str, target: Path, stage: dict, launcher: str | None,
                     port: int | None = None, *, managed: ManagedListener | None = None,
                     listener_pid: int | None = None, previous_selection: str | None = None) -> dict:
    prior_hash = sha256(previous_selection.encode("utf-8")).hexdigest() if previous_selection is not None else None
    if stage.get("previous_selection_sha256") != prior_hash:
        raise WorkspaceError("The previous update selection does not match the staged record.")
    old = Path(stage["source_workspace"])
    clone = Path(stage["candidate_workspace"])
    descriptor = target / "current.json"
    record = {"format": 1, "stage": stage}
    if managed is not None:
        record["managed"] = managed.binding

    def check_original() -> None:
        if workspace_fingerprint(old) != stage["source_fingerprint"]:
            raise WorkspaceError("The original workspace changed since staging.")

    def check_clone() -> None:
        if workspace_fingerprint(clone) != stage["candidate_fingerprint"]:
            raise WorkspaceError("The candidate workspace changed since staging.")

    def publish(state: str) -> dict:
        selection = record | {"state": state}
        publish_bytes(descriptor, (json.dumps(selection, indent=2) + "\n").encode())
        return selection

    def recorded_phase() -> str:
        current = read_selection(descriptor)
        phase = current.get("state")
        if current != (record | {"state": phase}):
            raise WorkspaceError("The update selection changed. Reconciliation is required.")
        return phase

    def rollback() -> dict:
        with maintenance_lock(clone, exclusive=True, create=False):
            if recorded_phase() not in {"pending", "selected", "ready", "reopening"}:
                raise WorkspaceError("Writes have opened. Reconciliation must preserve the candidate workspace.")
            check_original()
            check_clone()
            if previous_selection is None:
                descriptor.unlink()
            else:
                publish_bytes(descriptor, previous_selection.encode("utf-8"))
        return {"state": "original" if previous_selection is None else "open",
                "commit": stage["source_commit"]}

    if action == "recover" and recorded_phase() == "open":
        # The open descriptor is the point of no return even if no requests
        # have committed yet. Only verify the current candidate in place.
        with maintenance_lock(old, exclusive=True, create=False):
            check_original()
            doctor = diagnose_workspace(clone)
            if doctor.status == "maintenance":
                raise WorkspaceError("Stop the candidate listener before verifying the update.")
            if doctor.status != "initialized" or doctor.exit_status:
                raise WorkspaceError("The candidate failed stopped workspace verification.")
            check_health(clone)
        return {"state": "open", "commit": stage["candidate_commit"],
                "attachments_checked": doctor.attachments_checked}

    with maintenance_lock(old, exclusive=True, create=False):
        if action == "recover":
            return rollback()
        if action == "reopen":
            if recorded_phase() not in {"ready", "reopening"} or port is None:
                raise WorkspaceError("Select a healthy candidate before reopening writes. "
                                     "Use recovery to verify an open update.")
            with maintenance_lock(clone, exclusive=False, create=False):
                check_original()
                check_clone()
                publish("reopening")
                check_listener(port, stage, "reopening", listener_pid)
                if managed is not None and managed.read("reopening")[2] != listener_pid:
                    raise WorkspaceError("The supervised candidate listener changed. Writes remain closed.")
                check_clone()
                return publish("open")
        with maintenance_lock(clone, exclusive=True, create=False):
            check_original()
            check_clone()
            if verify_backup(Path(stage["backup"])) != ("Backup archive", stage["source_schema"]):
                raise WorkspaceError("The retained backup has the wrong type or schema.")
            if launcher is not None:
                publish_bytes(target / "repairlib", launcher.encode(), mode=0o700)
            if managed is not None:
                managed.adopt()
            publish("pending")
        try:
            # A normal app lifespan acquires its own shared candidate lock. Recheck
            # both fingerprints after each probe under exclusive locks before publication.
            for state in ("selected", "ready"):
                check_health(clone)
                with maintenance_lock(clone, exclusive=True, create=False):
                    check_original()
                    check_clone()
                    result = publish(state)
            return result
        except Exception:
            rollback()
            raise


def managed_selection(action: str, target: Path, stage: dict, launcher: str | None,
                      port: int, boots: list[str]) -> dict:
    listener = ManagedListener(target, stage, port, boots)
    if not private_control(listener.control, create=False):
        raise WorkspaceError("Register the existing RepairLib service before managed handoff.")
    descriptor = target / "current.json"
    with maintenance_lock(listener.control, exclusive=True, create=False):
        phase = None
        if os.path.lexists(descriptor):
            current = read_selection(descriptor)
            if current.get("managed") != listener.binding:
                raise WorkspaceError("Use the original managed unit and port for this update.")
            phase = current["state"]

        if action == "activate":
            listener.stop(None)
            listener.require_free_port()
            result = update_selection(action, target, stage, launcher, managed=listener)
            listener.start("ready")
            return result

        if action == "reopen":
            if phase not in {"ready", "reopening"}:
                raise WorkspaceError("Select a healthy candidate before reopening writes. "
                                     "Use recovery to verify an open update.")
            pid = listener.start(phase)
            return update_selection(action, target, stage, launcher, port, managed=listener, listener_pid=pid)

        if phase is None:
            result = {"state": "original", "commit": stage["source_commit"]}
        else:
            listener.stop(phase)
            result = update_selection(action, target, stage, launcher, managed=listener)
        listener.start("open" if result["state"] == "open" else None)
        return result


def exercise_synthetic_operation(workspace: Path) -> None:
    password = token_urlsafe(32)
    now = int(time())
    initialize_demo_workspace(workspace, username="owner", password=password)
    session = sessions.login(workspace, "owner", password, now=now)
    if session is None:
        raise WorkspaceError("Synthetic candidate sign-in failed.")
    token = tokens.create_token(workspace, session, password=password, name="Candidate rehearsal",
                                scope="write", key=token_hex(16), now=now)
    if token is None:
        raise WorkspaceError("Synthetic candidate credential check failed.")
    auth = {"Authorization": f"Bearer {token}"}
    app = create_app(workspace=workspace, require_initialized=True)
    with TestClient(app, base_url="http://localhost", headers=auth) as client:
        created = client.post("/api/v1/customers", json={"name": "Synthetic update check"},
                              headers={"Idempotency-Key": "candidate-customer"})
        if created.status_code != 201:
            raise WorkspaceError("Synthetic candidate customer creation failed.")
        customer_id = created.json()["id"]
    with TestClient(create_app(workspace=workspace, require_initialized=True),
                    base_url="http://localhost", headers=auth) as client:
        read = client.get(f"/api/v1/customers/{customer_id}")
        if read.status_code != 200 or read.json().get("name") != "Synthetic update check":
            raise WorkspaceError("Synthetic candidate customer readback failed.")


def rehearse(workspace: Path, candidate: Path, history: list[str]) -> dict:
    check_upgrade(history)
    check_space(workspace, candidate)
    backup = candidate / "backup.zip"
    clone = candidate / "workspace"
    with archived_workspace(workspace, backup, maximum_schema=len(history)) as source_schema:
        with TemporaryDirectory(prefix=".rehearsal-", dir=candidate) as temporary:
            if restore_archive(backup, Path(temporary) / "workspace") != source_schema:
                raise WorkspaceError("Restore rehearsal returned the wrong schema.")
        if restore_archive(backup, clone) != source_schema:
            raise WorkspaceError("Candidate clone returned the wrong schema.")
        try:
            migrate_workspace(clone)
        except (OSError, WorkspaceError) as error:
            raise WorkspaceError("Candidate migration failed. The verified backup is retained.") from error
        doctor = diagnose_workspace(clone)
        status = local_status(clone)
        if (doctor.status != "initialized" or doctor.exit_status or not status["ready"]
                or status["schema_version"] != SCHEMA_VERSION):
            raise WorkspaceError("Candidate workspace failed doctor or status checks.")
        check_health(clone)
        # Smoke-test writes under the staged runtime without introducing test data
        # into the retained copy. Neither ASGI client opens a listener.
        with TemporaryDirectory(prefix=".smoke-", dir=candidate) as temporary:
            exercise_synthetic_operation(Path(temporary) / "workspace")
        with maintenance_lock(clone, exclusive=True, create=False):
            candidate_fingerprint = workspace_fingerprint(clone)
        return {"source_schema": source_schema, "candidate_schema": SCHEMA_VERSION,
                "source_fingerprint": workspace_fingerprint(workspace),
                "candidate_fingerprint": candidate_fingerprint,
                "attachments_checked": doctor.attachments_checked, "checks": [
                    "backup-verified", "restore-rehearsed", "doctor", "health", "synthetic-customer-readback",
                ]}


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] in {"activate", "reopen", "recover"}:
            action, target = sys.argv[1:]
            request = json.load(sys.stdin)
            if request.get("managed_boots") is not None:
                result = managed_selection(action, Path(target), request["stage"], request["launcher"],
                                           request["port"], request["managed_boots"])
            else:
                result = update_selection(action, Path(target), request["stage"], request["launcher"],
                                          request.get("port"), previous_selection=request.get("previous_selection"))
        else:
            workspace, candidate = map(Path, sys.argv[1:])
            result = rehearse(workspace, candidate, json.load(sys.stdin)) | {"repeated_update_worker": True}
    except WorkspaceError as error:
        print(error)
        return 1
    except (OSError, RuntimeError, TypeError, ValueError):
        print("Could not complete candidate checks. Check workspace, runtime and disk access.")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
