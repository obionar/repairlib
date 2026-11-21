"""Support bundle: system facts only, never customer or repair content."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from repairlib.domain import system


def write_bundle(workspace: Path, output: Path, *, now: int | None = None) -> Path:
    if output.exists():
        raise FileExistsError(f"Refusing to replace {output}.")
    report = system.facts(workspace, now=now)
    text = "\n".join(system.lines(report)) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", ZIP_DEFLATED) as archive:
        for name, payload in (("status.json", json.dumps(report, indent=2, sort_keys=True)),
                              ("system.txt", text)):
            entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, payload)
    output.chmod(0o600)
    return output
