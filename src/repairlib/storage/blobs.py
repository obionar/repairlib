"""Original attachment bytes: staged streaming writes, opaque names and reconciliation."""

from collections.abc import Collection
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
import stat

from repairlib.paths import ACTIVE

FILES = "files"
STAGING = ".staging"
STORAGE_NAME_LENGTH = 32
STORAGE_NAME = re.compile(rf"[0-9a-f]{{{STORAGE_NAME_LENGTH}}}")
UPLOAD_PART = re.compile(rf"{STORAGE_NAME.pattern}\.part")


class BlobTooLarge(Exception):
    """The streamed content exceeded its byte limit."""


def files_root(workspace: Path) -> Path:
    return workspace / ACTIVE / FILES


def shop_directory(workspace: Path, shop_id: int) -> Path:
    return files_root(workspace) / f"shop-{shop_id}"


def blob_path(workspace: Path, shop_id: int, storage_name: str) -> Path:
    if not STORAGE_NAME.fullmatch(storage_name):
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
        # The first upload may also have created shop-N and files. Persist each
        # directory entry before SQLite can commit a reference to the blob.
        sync_directory(directory.parent)
        sync_directory(directory.parent.parent)
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


def managed_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()


def remove_unreferenced(directory: int, pattern: re.Pattern, known: Collection[str] = ()) -> int:
    removed = 0
    for name in os.listdir(directory):
        if not pattern.fullmatch(name) or name in known:
            continue
        if managed_file(os.stat(name, dir_fd=directory, follow_symlinks=False)):
            os.unlink(name, dir_fd=directory)
            removed += 1
    return removed


def file_digest(directory: int, name: str) -> str | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        if not managed_file(os.fstat(fd)):
            raise OSError("Recorded attachment is not a regular, unlinked file owned by the workspace user.")
        digest = sha256()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)
