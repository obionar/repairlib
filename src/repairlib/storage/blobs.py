"""Original attachment bytes: staged streaming writes, opaque names and reconciliation."""

from hashlib import sha256
import os
from pathlib import Path
import secrets

from repairlib.paths import ACTIVE

FILES = "files"
STAGING = ".staging"
STORAGE_NAME_LENGTH = 32


class BlobTooLarge(Exception):
    """The streamed content exceeded its byte limit."""


def files_root(workspace: Path) -> Path:
    return workspace / ACTIVE / FILES


def shop_directory(workspace: Path, shop_id: int) -> Path:
    return files_root(workspace) / f"shop-{shop_id}"


def blob_path(workspace: Path, shop_id: int, storage_name: str) -> Path:
    if len(storage_name) != STORAGE_NAME_LENGTH or not all(c in "0123456789abcdef" for c in storage_name):
        raise ValueError("Invalid storage name")
    return shop_directory(workspace, shop_id) / storage_name


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("Attachment storage directory is invalid.")


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StagedBlob:
    """Stream bytes into a private staging file, then publish under an opaque name."""

    def __init__(self, workspace: Path, limit: int):
        self.workspace = workspace
        self.limit = limit
        self.size = 0
        self.head = b""
        self.digest = sha256()
        self.final: Path | None = None
        staging = files_root(workspace) / STAGING
        private_directory(staging.parent)
        private_directory(staging)
        self.staged = staging / f"{secrets.token_hex(16)}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self.file = os.fdopen(os.open(self.staged, flags, 0o600), "wb")

    def write(self, chunk: bytes) -> None:
        if self.size + len(chunk) > self.limit:
            raise BlobTooLarge
        if len(self.head) < 16:
            self.head += chunk[:16 - len(self.head)]
        self.digest.update(chunk)
        self.file.write(chunk)
        self.size += len(chunk)

    def finish(self) -> None:
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()

    def publish(self, shop_id: int, *, storage_name: str | None = None) -> str:
        directory = shop_directory(self.workspace, shop_id)
        private_directory(directory.parent)
        private_directory(directory)
        storage_name = storage_name or secrets.token_hex(STORAGE_NAME_LENGTH // 2)
        final = blob_path(self.workspace, shop_id, storage_name)
        os.link(self.staged, final)  # refuses to replace an existing name
        self.final = final
        self.staged.unlink()
        sync_directory(directory)
        return storage_name

    def discard(self) -> None:
        if not self.file.closed:
            self.file.close()
        for path in (self.staged, self.final):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def keep(self) -> None:
        self.final = None


def remove_blob(workspace: Path, shop_id: int, storage_name: str) -> None:
    try:
        blob_path(workspace, shop_id, storage_name).unlink()
    except FileNotFoundError:
        pass


def stored_names(workspace: Path) -> dict[int, set[str]]:
    names = {}
    root = files_root(workspace)
    if not root.is_dir():
        return names
    for directory in root.iterdir():
        if directory.name.startswith("shop-") and directory.is_dir() and not directory.is_symlink():
            suffix = directory.name.removeprefix("shop-")
            if suffix.isdecimal():
                names[int(suffix)] = {path.name for path in directory.iterdir()}
    return names


def staging_files(workspace: Path) -> list[Path]:
    staging = files_root(workspace) / STAGING
    return sorted(staging.iterdir()) if staging.is_dir() else []


def file_digest(path: Path) -> str | None:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()
