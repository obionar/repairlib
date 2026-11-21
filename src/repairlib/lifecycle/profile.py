"""Read-only pre-write facts for agents: workspace, schema, backup, identity, server."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlopen

from repairlib.config import configured_base_url
from repairlib.lifecycle.backup_schedule import BACKUP_INTERVAL_SECONDS, backup_state
from repairlib.lifecycle.operations import local_status
from repairlib.lifecycle.workspace import database_path
from repairlib.storage import users

LOCAL_USER = "cli"


def server_ready(base_url: str) -> bool:
    try:
        with urlopen(f"{base_url.rstrip('/')}/health", timeout=3) as response:
            return response.status == 200 and json.loads(response.read(4096)).get("ready") is True
    except (OSError, ValueError):
        return False


def profile_check(workspace: Path) -> dict:
    """Collect the facts behind `profile check` without writing anything."""
    status = local_status(workspace)
    backup = backup_state(workspace)
    base_url = configured_base_url()
    blockers = []
    if not status["ready"]:
        blockers.append(f"Workspace is {status['status']}; run repairlib migrate or setup.")
    role = "admin"
    if status["ready"]:
        try:
            role = users.local_actor_role(database_path(workspace), LOCAL_USER) or role
        except users.LocalActorConflict as error:
            blockers.append(str(error))
    reachable = None if base_url is None else server_ready(base_url)
    if reachable is False:
        blockers.append(f"Server {base_url} is unreachable.")
    return {
        "workspace": status["workspace"],
        "state": status["status"],
        "schema_version": status["schema_version"],
        "expected_schema_version": status["expected_schema_version"],
        "backup_verified_today": (backup["age_seconds"] is not None
                                  and backup["age_seconds"] < BACKUP_INTERVAL_SECONDS),
        "backup_last_verified_at": backup["last_verified_at"],
        "identity": LOCAL_USER,
        "role": role,
        "server": base_url or "not configured",
        "server_ready": reachable,
        "blockers": blockers,
    }


def profile_lines(report: dict) -> list[str]:
    verified = "yes" if report["backup_verified_today"] else "no"
    if report["backup_last_verified_at"]:
        verified += f" ({report['backup_last_verified_at']})"
    return [
        f"Workspace: {report['workspace']}",
        f"State: {report['state']}",
        f"Schema: {report['schema_version'] or 'unavailable'} "
        f"(expected {report['expected_schema_version']})",
        f"Backup verified today: {verified}",
        f"Identity: {report['identity']}",
        f"Role: {report['role']}",
        f"Server: {report['server']}",
    ]
