"""Shop settings commands run against the local workspace."""

from dataclasses import asdict
import json
import sys

from repairlib.cli.common import add_revision, add_workspace, parse_cents_option
from repairlib import money
from repairlib.client import local


def register_settings_commands(commands) -> None:
    settings = commands.add_parser("settings", help="show or edit active shop settings")
    actions = settings.add_subparsers(dest="settings_action", required=True)
    show = actions.add_parser("show", help="show the active shop settings")
    edit = actions.add_parser("edit", help="edit reviewed shop settings")
    add_revision(edit)
    edit.add_argument("--key", required=True, help="unique request key reused on retry")
    edit.add_argument("--name")
    edit.add_argument("--timezone")
    edit.add_argument("--max-upload-mib", type=int)
    edit.add_argument("--inline-preview-mib", type=int)
    edit.add_argument("--language", help="default language code, for example en or el; empty clears it")
    edit.add_argument("--address", help="shop address printed on the handover; empty clears it")
    edit.add_argument("--phone", help="shop phone printed on the handover; empty clears it")
    edit.add_argument("--terms", help="terms printed on the handover; empty clears it")
    edit.add_argument("--diagnosis-fee", help="default bench diagnosis fee in EUR")
    for command in (show, edit):
        add_workspace(command)
        command.add_argument("--json", action="store_true", help="print machine-readable JSON")


def one_line(text: str) -> str:
    return " ".join(text.split())


def run_settings_command(args) -> int:
    try:
        ctx = local.open_workspace(args.workspace)
        if args.settings_action == "show":
            current, diagnosis_fee = local.show_settings_page(ctx)
        else:
            details = {"revision": args.revision}
            for name in ("name", "timezone", "max_upload_mib", "inline_preview_mib",
                         "language", "address", "phone", "terms"):
                if (value := getattr(args, name)) is not None:
                    details[name] = value
            diagnosis_fee = (None if args.diagnosis_fee is None
                             else parse_cents_option(args.diagnosis_fee, "--diagnosis-fee"))
            current, diagnosis_fee = local.edit_settings_page(ctx, details, args.key, diagnosis_fee_cents=diagnosis_fee)
        if args.json:
            print(json.dumps(asdict(current) | {"diagnosis_fee_cents": diagnosis_fee}, ensure_ascii=False))
        else:
            fee = "not recorded in this receipt" if diagnosis_fee is None else f"{money.format_cents(diagnosis_fee)} EUR"
            print(f"Shop\t{current.name}\nTimezone\t{current.timezone}\n"
                  f"Max upload\t{current.max_upload_mib} MiB\nInline preview\t{current.inline_preview_mib} MiB\n"
                  f"Language\t{current.language}\nAddress\t{one_line(current.address)}\n"
                  f"Phone\t{one_line(current.phone)}\nTerms\t{one_line(current.terms)}\n"
                  f"Diagnosis fee\t{fee}\n"
                  f"Revision\t{current.revision}")
    except (OSError, ValueError):
        print("settings: Could not read the workspace.", file=sys.stderr)
        return 1
    return 0
