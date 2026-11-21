"""Private workspace backup archives with bounded, read-only verification."""

import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager, nullcontext
from ctypes import CDLL, c_char_p, c_int, get_errno
from hashlib import sha256
from pathlib import Path
from struct import Struct, error as StructError
from tempfile import TemporaryDirectory
from typing import BinaryIO, TypedDict
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from repairlib.domain.attachments import MAX_UPLOAD_BYTES
from repairlib.lifecycle.operations import private_subdirectory
from repairlib.lifecycle.recovery import (
    publish_backup, require_private, staged_workspace_snapshot, verify_snapshot,
)
from repairlib.lifecycle.workspace import ACTIVE, DATABASE, LOCK, WorkspaceError, database_path
from repairlib.storage.blobs import FILES, STORAGE_NAME_LENGTH, blob_path
from repairlib.storage.initialization import SCHEMA_VERSION
from repairlib.storage.recovery import SnapshotError, prepare_restored_database, snapshot_attachments

FORMAT = "repairlib-backup-1"
RESTORE_STAGE_PREFIX = ".restore-"
MANIFEST = "manifest.json"
DB_MEMBER = f"{ACTIVE}/{DATABASE}"
ATTACHMENT_MEMBER = re.compile(
    rf"{ACTIVE}/{FILES}/shop-[1-9][0-9]{{0,18}}/[0-9a-f]{{{STORAGE_NAME_LENGTH}}}"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_ATTACHMENTS = 10_000  # test_attachment_cap keeps domain.attachments.MAX_RETAINED_ATTACHMENTS equal
MAX_MANIFEST = 2 << 20
MAX_DIRECTORY = 4 << 20
MAX_DATABASE = 64 << 30
MAX_PAYLOAD = 256 << 30
MAX_ARCHIVE = MAX_PAYLOAD + MAX_MANIFEST + 2 * MAX_DIRECTORY
CHUNK = 1 << 20
ZIP_HEADER = b"PK\x03\x04"
LOCAL_HEADER = Struct("<4s5H3I2H")
ZIP64_SIZES = Struct("<HHQQ")


class FileRecord(TypedDict):
    size: int
    sha256: str


class BoundedZipReader:
    """Cap the ZIP parser's bulk central-directory read before it allocates."""

    def __init__(self, handle: BinaryIO):
        self.handle = handle

    def read(self, size: int = -1) -> bytes:
        if size > MAX_DIRECTORY:
            raise WorkspaceError("Backup archive directory exceeds its limit.")
        if size < 0:
            data = self.handle.read(MAX_DIRECTORY + 1)
            if len(data) > MAX_DIRECTORY:
                raise WorkspaceError("Backup archive directory exceeds its limit.")
            return data
        return self.handle.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.handle.seek(offset, whence)

    def tell(self) -> int:
        return self.handle.tell()

    def seekable(self) -> bool:
        return self.handle.seekable()


@contextmanager
def private_file(path: Path | str, *, directory: int | None = None):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(path, flags, dir_fd=directory), "rb") as handle:
        require_private(os.fstat(handle.fileno()), stat.S_ISREG)
        yield handle


def file_limit(name: str) -> int:
    if name == DB_MEMBER:
        return MAX_DATABASE
    if ATTACHMENT_MEMBER.fullmatch(name):
        return MAX_UPLOAD_BYTES
    raise WorkspaceError("Backup archive contains an invalid file path.")


def check_size(name: str, size: int) -> None:
    if type(size) is not int or not 0 < size <= file_limit(name):
        raise WorkspaceError("Backup file size is invalid or exceeds its limit.")


def check_payload_limit(sizes) -> None:
    if sum(sizes) > MAX_PAYLOAD:
        raise WorkspaceError("Backup payload exceeds its total size limit.")


def attachment_inventory(database: Path, version: int) -> dict[str, FileRecord]:
    try:
        rows = snapshot_attachments(database, version, MAX_ATTACHMENTS + 1)
    except SnapshotError as error:
        raise WorkspaceError(str(error)) from error
    if len(rows) > MAX_ATTACHMENTS:
        raise WorkspaceError("Backup attachment count exceeds its limit.")
    files: dict[str, FileRecord] = {}
    for shop, storage, size, digest in rows:
        if type(shop) is not int or shop <= 0 or not isinstance(storage, str):
            raise WorkspaceError("Backup attachment metadata is invalid.")
        try:
            name = blob_path(Path(), shop, storage).as_posix()
        except ValueError as error:
            raise WorkspaceError("Backup attachment storage metadata is invalid.") from error
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or name in files:
            raise WorkspaceError("Backup attachment hash or storage metadata is invalid.")
        check_size(name, size)
        files[name] = {"size": size, "sha256": digest}
    return files


def hash_stream(source: BinaryIO, size: int, destination: BinaryIO | None = None) -> FileRecord:
    digest = sha256()
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, CHUNK))
        if not chunk:
            raise WorkspaceError("Backup file size does not match its declared size.")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise WorkspaceError("Backup file size does not match its declared size.")
    return {"size": size, "sha256": digest.hexdigest()}


def archive_entry(name: str, size: int) -> ZipInfo:
    info = ZipInfo(name)
    info.create_system = 3  # POSIX file modes
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.file_size = size
    return info


def write_archive(archive: ZipFile, database: Path, version: int, workspace: Path) -> None:
    files = attachment_inventory(database, version)
    size = database.stat().st_size
    check_size(DB_MEMBER, size)
    check_payload_limit([size, *(record["size"] for record in files.values())])
    with database.open("rb") as source, archive.open(archive_entry(DB_MEMBER, size), "w") as entry:
        manifest_files = {DB_MEMBER: hash_stream(source, size, entry)}

    for name, record in files.items():
        *directory, filename = name.split("/")
        size = record["size"]
        try:
            with private_subdirectory(workspace, *directory) as parent:
                if parent is None:
                    raise FileNotFoundError
                with private_file(filename, directory=parent) as source:
                    if os.fstat(source.fileno()).st_size != size:
                        raise WorkspaceError("Backup attachment size does not match SQLite metadata.")
                    with archive.open(archive_entry(name, size), "w") as entry:
                        if hash_stream(source, size, entry) != record:
                            raise WorkspaceError("Backup attachment hash does not match SQLite metadata.")
        except OSError as error:
            raise WorkspaceError("Could not safely read a retained backup attachment.") from error
    manifest_files.update(files)
    encoded = json.dumps({"format": FORMAT, "schema": version, "files": manifest_files},
                         indent=2).encode() + b"\n"
    if len(encoded) > MAX_MANIFEST:
        raise WorkspaceError("Backup manifest exceeds its size limit.")
    archive.writestr(archive_entry(MANIFEST, len(encoded)), encoded)


def regular_stored_entry(entry: ZipInfo) -> bool:
    return (entry.create_system == 3 and stat.S_ISREG(entry.external_attr >> 16)
            and not (entry.external_attr & 0x10 or entry.flag_bits or entry.volume)
            and entry.compress_type == ZIP_STORED and entry.compress_size == entry.file_size)


def archive_members(archive: ZipFile) -> dict[str, ZipInfo]:
    members: dict[str, ZipInfo] = {}
    if len(archive.infolist()) > MAX_ATTACHMENTS + 2:
        raise WorkspaceError("Backup archive entry count exceeds its limit.")
    for entry in archive.infolist():
        name = entry.orig_filename
        if entry.filename != name:
            raise WorkspaceError("Backup archive contains an invalid file path.")
        if name in members:
            raise WorkspaceError("Backup archive contains a duplicate file path.")
        if not regular_stored_entry(entry):
            raise WorkspaceError("Backup archive contains an unsupported entry.")
        if name == MANIFEST:
            if not 0 < entry.file_size <= MAX_MANIFEST:
                raise WorkspaceError("Backup manifest exceeds its size limit.")
        else:
            check_size(name, entry.file_size)
        members[name] = entry
    if MANIFEST not in members or DB_MEMBER not in members:
        raise WorkspaceError("Backup archive is missing its manifest or SQLite snapshot.")
    check_payload_limit(entry.file_size for name, entry in members.items() if name != MANIFEST)
    return members


def check_local_entries(handle: BinaryIO, archive: ZipFile, members: dict[str, ZipInfo]) -> None:
    """Reject hidden entries and gaps between the listed ZIP member records."""
    offset = 0
    for entry in sorted(members.values(), key=lambda entry: entry.header_offset):
        if entry.header_offset != offset or offset >= archive.start_dir:
            raise WorkspaceError("Backup archive contains unexpected entry data.")
        handle.seek(offset)
        signature, _, flags, compression, _, _, crc, stored, size, name_len, extra_len = (
            LOCAL_HEADER.unpack(handle.read(LOCAL_HEADER.size))
        )
        name = handle.read(name_len)
        extra = handle.read(extra_len)
        if extra == ZIP64_SIZES.pack(1, 16, entry.file_size, entry.file_size):
            if stored == size == 0xFFFFFFFF:
                stored = size = entry.file_size
                extra = b""
        if (signature != ZIP_HEADER or flags or compression != ZIP_STORED
                or crc != entry.CRC or stored != entry.file_size or size != entry.file_size
                or name != entry.orig_filename.encode("ascii") or extra):
            raise WorkspaceError("Backup archive contains an unsupported local entry.")
        offset = handle.tell() + stored
    if offset != archive.start_dir:
        raise WorkspaceError("Backup archive contains unexpected entry data.")


def unique_json_object(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise WorkspaceError("Backup manifest contains a duplicate key.")
    return result


def read_manifest(archive: ZipFile, members: dict[str, ZipInfo]) -> tuple[int, dict[str, FileRecord]]:
    try:
        manifest = json.loads(archive.read(members[MANIFEST]).decode("utf-8"),
                              object_pairs_hook=unique_json_object)
    except (ValueError, RecursionError) as error:
        raise WorkspaceError("Backup manifest is invalid JSON.") from error
    if (not isinstance(manifest, dict) or manifest.keys() != {"format", "schema", "files"}
            or manifest["format"] != FORMAT or type(manifest["schema"]) is not int
            or not isinstance(manifest["files"], dict)):
        raise WorkspaceError("Backup manifest has an unsupported format or invalid schema identity.")
    files = manifest["files"]
    if files.keys() != members.keys() - {MANIFEST}:
        raise WorkspaceError("Backup archive membership does not match the manifest.")
    for name, record in files.items():
        if (not isinstance(record, dict) or record.keys() != {"size", "sha256"}
                or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"])):
            raise WorkspaceError("Backup manifest contains invalid file metadata.")
        check_size(name, record["size"])
        if record["size"] != members[name].file_size:
            raise WorkspaceError("Backup file size does not match the manifest.")
    return manifest["schema"], files


def verify_archive(handle: BinaryIO, staging: Path, *, restore: bool = False) -> int:
    """Verify all members, keeping attachment bytes in staging only for restore."""
    if os.fstat(handle.fileno()).st_size > MAX_ARCHIVE:
        raise WorkspaceError("Backup archive exceeds its size limit.")
    database = database_path(staging) if restore else staging / DATABASE
    try:
        with ZipFile(BoundedZipReader(handle)) as archive:
            members = archive_members(archive)
            check_local_entries(handle, archive, members)
            declared_version, files = read_manifest(archive, members)
            if restore:
                database.parent.mkdir(mode=0o700)
            database.touch(mode=0o600, exist_ok=False)
            with database.open("wb") as destination, archive.open(members[DB_MEMBER]) as source:
                if hash_stream(source, files[DB_MEMBER]["size"], destination) != files[DB_MEMBER]:
                    raise WorkspaceError("Backup SQLite hash does not match the manifest.")
            version = verify_snapshot(database)
            if declared_version != version:
                raise WorkspaceError("Backup schema does not match the SQLite snapshot.")
            attachments = {name: record for name, record in files.items() if name != DB_MEMBER}
            if attachments != attachment_inventory(database, version):
                raise WorkspaceError("Backup attachment files do not match SQLite metadata.")
            if restore and attachments:
                (database.parent / FILES).mkdir(mode=0o700)
            for name, record in attachments.items():
                output = nullcontext()
                if restore:
                    path = staging / name
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                    path.touch(mode=0o600, exist_ok=False)
                    output = path.open("wb")
                with output as destination, archive.open(members[name]) as source:
                    if hash_stream(source, record["size"], destination) != record:
                        raise WorkspaceError("Backup attachment hash does not match the manifest.")
    except (BadZipFile, NotImplementedError, StructError, UnicodeError, EOFError, OverflowError) as error:
        raise WorkspaceError("Backup archive has invalid or unsupported ZIP data.") from error
    return version


@contextmanager
def archived_workspace(workspace: Path, output: Path, *, maximum_schema: int | None = None,
                       lock_fd: int | None = None):
    """Publish a verified backup and hold the source lock through its caller's rehearsal."""
    with staged_workspace_snapshot(workspace, output, database_only=False,
                                   lock_fd=lock_fd) as (database, version, output):
        if maximum_schema is not None and version > maximum_schema:
            raise WorkspaceError("Workspace schema is newer than the installed runtime supports.")
        staged = database.with_name("backup.zip")
        staged.touch(mode=0o600, exist_ok=False)
        with ZipFile(staged, "w") as archive:
            write_archive(archive, database, version, workspace)
        database.unlink()
        with staged.open("rb") as handle:
            verify_archive(handle, database.parent)
        publish_backup(staged, output)
        yield version


def create_archive(workspace: Path, output: Path) -> int:
    with archived_workspace(workspace, output) as version:
        return version


def verify_backup(path: Path) -> tuple[str, int]:
    require_private(path.parent.lstat(), stat.S_ISDIR)
    with private_file(path) as handle:
        if handle.read(len(ZIP_HEADER)) == ZIP_HEADER:
            handle.seek(0)
            with TemporaryDirectory(prefix=".verify-", dir=path.parent) as temporary:
                return "Backup archive", verify_archive(handle, Path(temporary))
    return "SQLite snapshot", verify_snapshot(path)


def verify_restore_point(path: Path, schema: int) -> str:
    """Refuse unless `path` verifies and holds the given pre-upgrade schema."""
    try:
        kind, version = verify_backup(path)
    except FileNotFoundError as error:
        raise WorkspaceError("Backup archive not found.") from error
    except OSError as error:
        raise WorkspaceError("Backup archive could not be read safely.") from error
    if version != schema:
        raise WorkspaceError(f"Backup schema {version} does not match workspace schema {schema}.")
    return f"{kind} verified (schema {version}): {path}"


def publish_restore(parent_fd: int, staged: str, destination: str) -> None:
    """Commit a complete directory without replacing even an empty directory."""
    try:
        rename = CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise WorkspaceError("Atomic no-replace workspace publication is unavailable.") from error
    rename.argtypes = (c_int, c_char_p, c_int, c_char_p, c_int)
    rename.restype = c_int
    rename_noreplace = 1  # Linux RENAME_NOREPLACE
    if rename(parent_fd, os.fsencode(staged), parent_fd, os.fsencode(destination), rename_noreplace):
        error = get_errno()
        raise OSError(error, os.strerror(error))


def restore_archive(artifact: Path, destination: Path) -> int:
    if sys.platform != "linux":
        raise WorkspaceError("Safe workspace restore currently requires Linux.")
    destination = destination.absolute()
    parent = destination.parent
    if not destination.name or ".." in destination.parts:
        raise WorkspaceError("Restore requires a new workspace name and a real, private parent directory.")
    with ExitStack() as stack:
        try:
            parent_fd = stack.enter_context(private_subdirectory(Path(parent.anchor), *parent.parts[1:]))
        except OSError as error:
            raise WorkspaceError("Restore requires a real parent directory without symbolic links.") from error
        if parent_fd is None:
            raise WorkspaceError("Restore requires an existing parent directory.")
        require_private(os.fstat(parent_fd), stat.S_ISDIR)
        # Anchor staging and its cleanup to the same directory as the commit.
        anchored_parent = Path("/proc/self/fd") / str(parent_fd)
        if os.path.lexists(anchored_parent / destination.name):
            raise WorkspaceError("Restore destination already exists.")
        require_private(artifact.parent.lstat(), stat.S_ISDIR)
        with private_file(artifact) as handle:
            if handle.read(len(ZIP_HEADER)) != ZIP_HEADER:
                raise WorkspaceError("Restore requires a full workspace backup archive.")
            handle.seek(0)
            holder = stack.enter_context(TemporaryDirectory(
                prefix=RESTORE_STAGE_PREFIX, dir=anchored_parent, ignore_cleanup_errors=True,
            ))
            staged = Path(holder) / "workspace"
            staged.mkdir(mode=0o700)
            version = verify_archive(handle, staged, restore=True)
        if version == SCHEMA_VERSION:
            try:
                prepare_restored_database(database_path(staged))
            except SnapshotError as error:
                raise WorkspaceError(str(error)) from error
        (staged / LOCK).touch(mode=0o600, exist_ok=False)
        # The lock file moves with the state, so normal runtime locking works
        # as soon as the destination becomes visible.
        try:
            if not os.path.samestat(parent.lstat(), os.fstat(parent_fd)):
                raise WorkspaceError("Restore destination parent changed during staging.")
            publish_restore(parent_fd, str(staged.relative_to(anchored_parent)), destination.name)
        except FileExistsError as error:
            raise WorkspaceError("Restore destination already exists.") from error
        except OSError as error:
            raise WorkspaceError("Could not publish the restored workspace.") from error
        return version
