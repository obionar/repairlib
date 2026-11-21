"""`--dry-run`: run a covered mutation for real inside a transaction that is always rolled back."""

from contextlib import redirect_stdout
import io
import json

from repairlib.client.local import ClientError
from repairlib.domain import dry_run

# (command, action) pairs whose domain operation writes only through the storage transactions.
COVERED = {
    ("customers", "create"), ("repairs", "create"), ("repairs", "stage"),
    ("repairs", "add-charge"), ("repairs", "pickup"),
}


def action_of(args) -> str | None:
    return getattr(args, {"customers": "customer_action", "repairs": "repair_action"}.get(args.command, ""), None)


def check(args) -> None:
    if (args.command, action_of(args)) not in COVERED:
        name = " ".join(part for part in (args.command, action_of(args)) if part)
        raise ClientError(f"--dry-run does not support '{name}'; nothing was run. "
                          "It covers customers create, repairs create, stage, add-charge and pickup.", 2,
                          code="dry_run_unsupported")


def field_names(changes: dict) -> list[str]:
    """Names the audit row records: a `fields` list or dict, plus every other changed key."""
    listed = changes.get("fields", ())
    return [*listed, *(name for name in changes if name != "fields")]


def describe(event: dict) -> str:
    target = "new record" if event["entity_id"] is None else f"{event['entity_type']} {event['entity_id']}"
    return f"{event['operation']}\t{target}\t{', '.join(field_names(event['changes'])) or '-'}"


def report(event: dict) -> dict:
    """The audit event as it would be stored; a created record has no id until it is really written."""
    created = event["operation"].endswith(".create")
    return {**event, "entity_id": None if created else event["entity_id"]}


def run(args, operation) -> int:
    """Run `operation` armed, mute its normal output and print what it would have changed."""
    with dry_run.armed() as session:
        with redirect_stdout(io.StringIO()):
            status = operation(args)
        events = [report(event) for event in session.events()]
    if status != 0:
        return status
    if args.json:
        print(json.dumps({"dry_run": True, "events": events}))
    else:
        for event in events:
            print(f"Dry run: {describe(event)}")
        print("No changes were written.")
    return 0
