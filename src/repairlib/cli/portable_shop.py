"""Portable archive commands run against the local workspace."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory

from repairlib.cli.common import add_revision, add_workspace
from repairlib.client import local
from repairlib.contracts import PortablePreview
from repairlib.lifecycle.recovery import publish_backup, require_private
from repairlib.lifecycle.workspace import WorkspaceError


def register_portable_commands(commands) -> None:
    portable = commands.add_parser("portable", help="export, preview or import a portable shop archive")
    actions = portable.add_subparsers(dest="portable_action", required=True)
    export = actions.add_parser("export", help="download the current shop to a new private ZIP file")
    preview = actions.add_parser("preview", help="verify an archive and show its source and counts")
    apply = actions.add_parser("import", help="import an archive into the confirmed empty shop")
    apply.add_argument("--destination-shop-id", type=int, required=True)
    add_revision(apply, help="reviewed destination settings revision")
    apply.add_argument("--key", required=True, help="unique request key reused on retry")
    for command in (export, preview, apply):
        command.add_argument("archive", type=Path)
        add_workspace(command)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def save_export(ctx, output: Path) -> dict:
    if os.name != "posix":
        raise local.ClientError("Private archive output requires POSIX file permissions.", 3)
    if output.parent.is_symlink():
        raise local.ClientError("Choose a real, private archive output directory.", 8)
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    require_private(parent.stat(), stat.S_ISDIR)
    if os.path.lexists(output):
        raise local.ClientError("The archive output already exists. Choose a new path.", 7)
    with TemporaryDirectory(prefix=".repairlib-portable-", dir=parent) as staging:
        staged = Path(staging) / "shop.zip"
        fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            size = local.download_portable_shop(ctx, handle)
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            if mode != 0o600:
                os.fchmod(handle.fileno(), 0o600)
        publish_backup(staged, output)
    return {"archive": str(output), "size": size}


def upload_file(ctx, path: Path, operation, **options) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise local.ClientError("Choose a regular portable archive file.", 8)
        return asdict(operation(ctx, handle, info.st_size, **options))


def run_portable_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        archive = args.archive.expanduser()
        if args.portable_action == "export":
            result = save_export(ctx, archive)
            summary = f"Saved private portable archive to {result['archive']} ({result['size']} bytes)."
        elif args.portable_action == "import":
            result = upload_file(ctx, archive, local.import_portable_shop,
                                 destination_shop_id=args.destination_shop_id, revision=args.revision, key=args.key)
            summary = f"Imported archive into shop {result['destination_shop_id']}."
        else:
            result = upload_file(ctx, archive, local.preview_portable_shop)
            source = result["source_shop"]
            summary = "\n".join([
                f"Source shop\t{source['name']} ({source['id']})",
                f"Format\t{result['format']}",
                f"Schema\t{result['schema']}",
                f"Retained attachments\t{result['attachment_files']} ({result['attachment_bytes']} bytes)",
                *(f"{table}\t{count}" for table, count in result["counts"].items()),
            ])
        print(json.dumps(result, ensure_ascii=False) if args.json else summary)
        if args.portable_action != "preview" or not args.json:
            print("Format 1 does not carry: " + "; ".join(PortablePreview.omitted)
                  + ". For a complete move use a full backup.", file=sys.stderr)
    except WorkspaceError:
        print("portable: The workspace is not available.", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("portable: Could not read or write the archive path.", file=sys.stderr)
        return 8
    return 0
