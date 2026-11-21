"""Process-local pristine synthetic workspaces for non-auth tests."""

from atexit import register
from functools import cache
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

from repairlib.domain.setup import initialize_workspace


@cache
def _template(password: str) -> Path:
    directory = TemporaryDirectory(prefix="repairlib-fixture-")
    register(directory.cleanup)
    workspace = Path(directory.name) / "template"
    initialize_workspace(workspace, shop="Synthetic", timezone="UTC", username="owner",
                         password=password)
    return workspace


def clone_workspace(destination: Path, password: str) -> None:
    copytree(_template(password), destination)
