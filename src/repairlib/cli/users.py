"""User administration commands run against the local workspace."""

from dataclasses import asdict
import getpass
import json
import sys

from repairlib.cli.common import add_revision, add_workspace, display_name, read_password
from repairlib.client import local

ROLES = ("admin", "technician", "viewer")


def register_user_commands(commands) -> None:
    user_commands = commands.add_parser("users", help="list, create and edit shop users")
    actions = user_commands.add_subparsers(dest="user_action", required=True)
    listing = actions.add_parser("list", help="list a page of users")
    listing.add_argument("--limit", default=25, type=int)
    listing.add_argument("--before", type=int, help="cursor from the preceding page")
    create = actions.add_parser("create", help="create a user; read the password securely")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=ROLES, default="technician")
    create.add_argument("--key", required=True, help="unique request key reused on retry")
    show = actions.add_parser("show", help="show a user and current revision")
    edit = actions.add_parser("edit", help="edit a reviewed user")
    for command in (show, edit):
        command.add_argument("id", type=int)
    add_revision(edit)
    edit.add_argument("--key", required=True, help="unique request key reused on retry")
    edit.add_argument("--username")
    edit.add_argument("--role", choices=ROLES)
    status = edit.add_mutually_exclusive_group()
    status.add_argument("--active", action="store_true", default=None)
    status.add_argument("--disabled", action="store_true")
    edit.add_argument("--password", action="store_true", help="read a new password securely")
    for command in (listing, create, show, edit):
        add_workspace(command)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")

    passwd = commands.add_parser("passwd", aliases=["recover-password"],
                                 help="set a user's password from this shell; ends their sessions and tokens")
    passwd.add_argument("--username", required=True)
    add_workspace(passwd)


def run_passwd_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        user = local.set_password(ctx, args.username, new_password())
    except (OSError, EOFError, getpass.GetPassWarning):
        print("passwd: Could not read the workspace or new password.", file=sys.stderr)
        return 1
    print(f"Password set for {display_name(user.username)}; their other sessions and tokens are revoked.")
    return 0


def new_password() -> str:
    return read_password("New user password: ")


def run_user_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.user_action == "list":
            page = local.list_users(ctx, limit=args.limit, before=args.before)
            if args.json:
                print(json.dumps(asdict(page)))
            else:
                for user in page.items:
                    state = "active" if user.active else "disabled"
                    print(f"{user.id}\t{display_name(user.username)}\t{user.role}\t{state}")
                if page.next_before is not None:
                    print(f"Next page: --before {page.next_before}", file=sys.stderr)
        elif args.user_action == "create":
            details = {"username": args.username, "role": args.role, "password": new_password()}
            user = local.create_user(ctx, details, args.key)
            print(json.dumps(asdict(user)) if args.json else f"Created user {user.id}.")
        elif args.user_action == "show":
            user = local.show_user(ctx, args.id)
            print(json.dumps(asdict(user)) if args.json else
                  f"{user.id}\t{display_name(user.username)}\t{user.role}\tRevision {user.revision}")
        else:
            details = {"revision": args.revision}
            for name in ("username", "role"):
                if (value := getattr(args, name)) is not None:
                    details[name] = value
            if args.active:
                details["active"] = True
            elif args.disabled:
                details["active"] = False
            if args.password:
                details["password"] = new_password()
            user = local.edit_user(ctx, args.id, details, args.key)
            print(json.dumps(asdict(user)) if args.json else
                  f"Saved user {user.id} at revision {user.revision}.")
    except (OSError, ValueError, EOFError, getpass.GetPassWarning):
        print("users: Could not read the workspace or new password.", file=sys.stderr)
        return 1
    return 0
