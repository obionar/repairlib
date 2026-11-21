"""System facts for the admin page and the support bundle: never business rows."""

from contextlib import closing
from datetime import datetime
from pathlib import Path
import time

from repairlib.lifecycle.backup_schedule import backup_state
from repairlib.lifecycle.operations import local_status
from repairlib.paths import database_path
from repairlib.storage import audit, printer as printer_records
from repairlib.domain.users import authorized
from repairlib.storage.sessions import active_count, connect

CLOCK_TOLERANCE_SECONDS = 300


def skew_seconds(newest: str | None, now: int) -> int | None:
    """Seconds the system clock trails the newest stored event; None when unknown."""
    if not newest:
        return None
    try:
        recorded = int(datetime.fromisoformat(newest).timestamp())
    except ValueError:
        return None
    return max(0, recorded - now)


def facts(workspace: Path, *, now: int | None = None) -> dict:
    moment = int(time.time()) if now is None else now
    report = local_status(workspace)
    backup = backup_state(workspace, now=moment)
    printer, sessions, newest = "none", 0, None
    if report["ready"]:
        path = database_path(workspace)
        with closing(connect(path, "ro")) as db:
            printer, _ = printer_records.configuration(db, 1)
            sessions = active_count(db, 1, moment)
            newest = audit.newest_occurred_at(db)
    skew = skew_seconds(newest, moment)
    return {
        "now": moment,
        "status": report["status"],
        "schema_version": report["schema_version"],
        "expected_schema_version": report["expected_schema_version"],
        "workspace": report["workspace"],
        "disk_free_bytes": report["disk_free_bytes"],
        "backup": backup,
        "printer": printer or "none",
        "sessions": sessions,
        "clock_skew_seconds": skew,
        "clock_warning": skew is not None and skew > CLOCK_TOLERANCE_SECONDS,
    }


def read_system(workspace: Path, access, *, now: int | None = None) -> dict:
    """Admin-only view of the same facts; the page and the bundle share them."""
    with authorized(workspace, access, capability="read the system page"):
        return facts(workspace, now=now)


def lines(report: dict) -> list[str]:
    """Plain diagnostic text for a support bundle; English on purpose."""
    free = report["disk_free_bytes"]
    disk = "unknown" if free is None else f"{free // (1024 * 1024)} MiB free"
    backup = report["backup"]
    last = backup["last_verified_at"] or "none"
    clock = "ok" if not report["clock_warning"] else (
        f"clock trails the newest record by {report['clock_skew_seconds']} s")
    return [
        f"Status: {report['status']}",
        f"Schema: {report['schema_version']} (expected {report['expected_schema_version']})",
        f"Workspace: {report['workspace']}",
        f"Disk: {disk}",
        f"Last verified backup: {last}",
        f"Printer: {report['printer']}",
        f"Sessions: {report['sessions']}",
        f"Clock: {clock}",
    ]
