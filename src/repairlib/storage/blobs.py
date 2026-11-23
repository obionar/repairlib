"""Original attachment bytes: staged streaming writes, opaque names and reconciliation."""

from collections.abc import Collection
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
import stat

from repairlib.paths import ACTIVE, sync_directory

FILES = "files"
STAGING = ".staging"
STORAGE_NAME_LENGTH = 32
BLOB_NAME = re.compile(rf"[0-9a-f]{{{STORAGE_NAME_LENGTH}}}")
IMAGES = "images"
IMAGE_FOLDER = re.compile(r"[1-9][0-9]{0,18}")
# Images live under images/<repair id>/; older images and other files stay flat in shop-N.
STORAGE_NAME = re.compile(rf"(?:{IMAGES}/{IMAGE_FOLDER.pattern}/)?{BLOB_NAME.pattern}")
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
UPLOAD_PART = re.compile(rf"{STORAGE_NAME.pattern}\.part")


class BlobTooLarge(Exception):
    """The streamed content exceeded its byte limit."""


def files_root(workspace: Path) -> Path:
    return workspace / ACTIVE / FILES


def blob_parts(shop_id: int, storage_name: str) -> tuple[str, ...]:
    """Directory components below files/, then the blob name; refuses anything else."""
    if not STORAGE_NAME.fullmatch(storage_name):
        raise ValueError("Invalid storage name")
    return (f"shop-{shop_id}", *storage_name.split("/"))


def blob_path(workspace: Path, shop_id: int, storage_name: str) -> Path:
    return files_root(workspace).joinpath(*blob_parts(shop_id, storage_name))


SHOP_DIRECTORY = re.compile(r"shop-[1-9][0-9]*")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def blob_directories(files: int) -> list[tuple[str, ...]]:
    """Shop and image folders below the open files/ directory; symlinked levels raise OSError."""
    found = []
    for shop in sorted(os.listdir(files)):
        if not SHOP_DIRECTORY.fullmatch(shop):
            continue
        found.append((shop,))
        shop_fd = os.open(shop, DIRECTORY_FLAGS, dir_fd=files)
        try:
            try:
                images = os.open(IMAGES, DIRECTORY_FLAGS, dir_fd=shop_fd)
            except FileNotFoundError:
                continue
            try:
                found.extend((shop, IMAGES, name) for name in sorted(os.listdir(images))
                             if IMAGE_FOLDER.fullmatch(name))
            finally:
                os.close(images)
        finally:
            os.close(shop_fd)
    return found


def image_folder(media_type: str, repair_id: int) -> str:
    return f"{IMAGES}/{repair_id}/" if media_type in IMAGE_TYPES else ""


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("Attachment storage directory is invalid.")


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

    def publish(self, shop_id: int, *, storage_name: str | None = None, folder: str = "") -> str:
        storage_name = storage_name or folder + secrets.token_hex(STORAGE_NAME_LENGTH // 2)
        final = blob_path(self.workspace, shop_id, storage_name)
        root = files_root(self.workspace)
        created = [root, *final.parents[:len(final.relative_to(root).parts) - 1][::-1]]
        for directory in created:
            private_directory(directory)
        os.link(self.staged, final)  # refuses to replace an existing name
        self.final = final
        self.staged.unlink()
        # The first upload may also have created files, shop-N and image folders. Persist
        # each directory entry, leaf first, before SQLite can commit a reference to the blob.
        for directory in reversed([root.parent, *created]):
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


def resolve_link_pairs(files: int, referenced: Collection[tuple[tuple[str, ...], str]]) -> tuple[int, int, int]:
    """Clear the two-name state left when publication stopped between `link` and `unlink`.

    A staging name whose other name is a managed blob in this workspace (same device and inode, exactly
    two names, ours) is that state. If the database references the blob only the staging name goes;
    otherwise the upload was never acknowledged and both go. Any other hard link is left alone and
    counted. Returns (staging removed, orphans removed, unresolved).
    """
    try:
        stages = os.open(STAGING, DIRECTORY_FLAGS, dir_fd=files)
    except FileNotFoundError:
        return 0, 0, 0
    try:
        staged = {}
        for name in os.listdir(stages):
            info = os.stat(name, dir_fd=stages, follow_symlinks=False)
            if UPLOAD_PART.fullmatch(name) and stat.S_ISREG(info.st_mode) and info.st_nlink == 2 \
                    and info.st_uid == os.geteuid():
                staged[(info.st_dev, info.st_ino)] = name
        removed_staging = removed_orphans = 0
        for directory in blob_directories(files) if staged else ():
            fd = files
            opened = []
            try:
                for part in directory:
                    fd = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
                    opened.append(fd)
                for name in os.listdir(fd):
                    if not BLOB_NAME.fullmatch(name):
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    twin = staged.get((info.st_dev, info.st_ino))
                    if twin is None or not (stat.S_ISREG(info.st_mode) and info.st_nlink == 2
                                            and info.st_uid == os.geteuid()):
                        continue
                    os.unlink(twin, dir_fd=stages)
                    removed_staging += 1
                    del staged[(info.st_dev, info.st_ino)]
                    if (directory, name) not in referenced:
                        os.unlink(name, dir_fd=fd)
                        removed_orphans += 1
            finally:
                for opened_fd in opened:
                    os.close(opened_fd)
        return removed_staging, removed_orphans, len(staged)
    finally:
        os.close(stages)


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
