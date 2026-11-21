"""Repair intake, active technician assignment and attributed work entries."""

from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from repairlib.contracts import (
    OPEN_REPAIR_STAGES, TERMINAL_REPAIR_STAGES, DeviceRepair, Repair, RepairFlagChange, RepairInput,
    RepairActivityPage, RepairPage, RepairPatch, StageChange,
    WorkEntry, WorkEntryCorrection, WorkEntryInput, WorkEntryPage, WorkEntryVisibility,
)
from repairlib.domain.customers import MAX_ID, CustomerAccess, CustomerError, authorized, search_pattern
from repairlib.domain.replay import replay as replay_receipt
from repairlib.search import phone_search_pattern
from repairlib.storage import repairs

RepairAccess = CustomerAccess
RepairError = CustomerError
FIELD_LIMITS = {
    "device_type": 80, "brand": 80, "model": 120, "serial": 120,
    "display_device": 160, "reported_issue": 2000, "notes": 4000,
}
ENTRY_KINDS = {"work", "test", "contact", "handover", "note"}
STAGE_TRANSITIONS = {
    "new": {"in_progress", "waiting_parts", "lab_test", "no_repair"},
    "in_progress": {"waiting_parts", "lab_test", "customer_test", "repaired", "no_repair"},
    "waiting_parts": {"in_progress", "lab_test", "no_repair"},
    "lab_test": {"in_progress", "waiting_parts", "customer_test", "repaired", "no_repair"},
    "customer_test": {"in_progress", "repaired", "no_repair"},
    "repaired": {"in_progress"},
    "no_repair": {"in_progress"},
}
REPAIR_QUEUES = {"open": OPEN_REPAIR_STAGES, "pickup": TERMINAL_REPAIR_STAGES}


def normalize_fields(details: dict) -> dict:
    values = {}
    errors = {}
    for name, raw in details.items():
        limit = FIELD_LIMITS[name]
        value = raw.strip().replace("\r\n", "\n")
        values[name] = value
        if name == "reported_issue" and not value:
            errors[name] = "Describe the reported issue."
        elif len(value) > limit:
            errors[name] = f"Use at most {limit} characters."
        elif any(not char.isprintable() and (name not in {"reported_issue", "notes"}
                                             or char not in "\n\t") for char in value):
            errors[name] = "Remove unsupported characters."
    if errors:
        raise RepairError(422, "validation", "Check the repair fields.", errors)
    return values


def normalize_relationship(name: str, value: int | None) -> int | None:
    if value is not None and not 1 <= value <= MAX_ID:
        raise RepairError(422, "validation", "Check the repair relationships.", {name: "Invalid selection."})
    return value


def normalize_warranty_until(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return ""
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise RepairError(422, "validation", "Check the repair fields.",
                      {"warranty_until": "Enter a valid date in YYYY-MM-DD format."})


def warranty_is_active(until: str, timezone: str) -> bool:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = UTC
    return datetime.now(UTC).astimezone(zone).date() <= date.fromisoformat(until)


def normalized_input(details: RepairInput) -> RepairInput:
    return RepairInput(
        customer_id=normalize_relationship("customer_id", details.customer_id),
        technician_id=normalize_relationship("technician_id", details.technician_id),
        warranty_until=normalize_warranty_until(details.warranty_until),
        warranty_repair_id=normalize_relationship("warranty_repair_id", details.warranty_repair_id),
        **normalize_fields({name: getattr(details, name) for name in FIELD_LIMITS}),
    )


def check_relationships(db, shop_id: int, values: RepairInput, *, current: Repair | None = None) -> None:
    if values.customer_id is not None and not repairs.customer_is_selectable(
        db, shop_id, values.customer_id, current.customer_id if current else None,
    ):
        raise RepairError(422, "validation", "Check the repair relationships.",
                          {"customer_id": "Choose a customer from this shop."})
    if (values.technician_id is not None and (current is None or values.technician_id != current.technician_id)
            and not repairs.active_technician(db, shop_id, values.technician_id)):
        raise RepairError(422, "validation", "Check the repair relationships.",
                          {"technician_id": "Choose an active technician from this shop."})
    link = values.warranty_repair_id
    if link is not None and (current is None or link != current.warranty_repair_id):
        if current is not None and link == current.id:
            raise RepairError(422, "validation", "Check the repair relationships.",
                              {"warranty_repair_id": "A repair cannot be its own warranty return."})
        earlier = repairs.find(db, shop_id, link)
        if earlier is not None and current is not None and earlier.id > current.id:
            earlier = None  # a later repair cannot be the origin; this also rules out cycles
        if earlier is None:
            raise RepairError(422, "validation", "Check the repair relationships.",
                              {"warranty_repair_id": "Choose an earlier repair from this shop."})
        if earlier.archived_at is not None:
            raise RepairError(422, "validation", "Check the repair relationships.",
                              {"warranty_repair_id": "Restore the archived repair before linking it."})


def replay(db, principal, operation: str, key: str, payload: dict, shape):
    return replay_receipt(db, principal, "repair", operation, key, payload, lambda values: shape(**values),
                          RepairError)


def repair_fields(values: RepairInput) -> dict:
    fields = asdict(values)
    # Requests without a warranty must retain their pre-migration fingerprints.
    if values.warranty_until is None:
        del fields["warranty_until"]
    if values.warranty_repair_id is None:
        del fields["warranty_repair_id"]
    return fields


def create_repair(workspace: Path, access: RepairAccess, details: RepairInput, *, key: str) -> Repair:
    values = normalized_input(details)
    values = replace(values, warranty_until=values.warranty_until or None)
    fields = repair_fields(values)
    operation = "repair.create"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        fingerprint, previous = replay(db, principal, operation, key, fields, Repair)
        if previous is not None:
            return previous
        check_relationships(db, principal.shop_id, values)
        created = repairs.insert(db, principal.shop_id, values)
        repairs.save_result(db, principal.shop_id, principal.user_id, operation, key,
                            fingerprint, created.id, created)
        repairs.audit(db, principal, access.source, token_label, operation, "repair", created.id,
                      {"fields": list(fields), "stage": "new"})
        return created


def find_repair(db, shop_id: int, repair_id: int) -> Repair:
    record = repairs.find(db, shop_id, repair_id) if 1 <= repair_id <= MAX_ID else None
    if record is None:
        raise RepairError(404, "not_found", "Repair not found.")
    return record


def find_unarchived_repair(db, shop_id: int, repair_id: int) -> Repair:
    record = find_repair(db, shop_id, repair_id)
    if record.archived_at is not None:
        raise RepairError(409, "archived", "Restore the archived repair before changing it.")
    return record


def list_repairs(workspace: Path, access: RepairAccess, *, before: int | None = None,
                 limit: int = 25, query: str = "", stage: str | None = None,
                 picked_up: bool | None = None, archived: bool | None = None,
                 technician: str = "", customer_id: int | None = None, queue: str = "",
                 paid: bool | None = None):
    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
        raise RepairError(422, "validation", "Invalid repair page.")
    if stage is not None and stage not in STAGE_TRANSITIONS:
        raise RepairError(422, "validation", "Choose a valid stage filter.", {"stage": "Unknown stage."})
    if queue and queue not in REPAIR_QUEUES:
        raise RepairError(422, "validation", "Choose a valid repair queue.")
    if customer_id is not None and not 1 <= customer_id <= MAX_ID:
        raise RepairError(422, "validation", "Supply a valid customer ID.")
    technician_id = None
    if technician not in {"", "me", "unassigned"}:
        if not (len(technician) <= 19 and technician.isascii() and technician.isdecimal()
                and 1 <= int(technician) <= MAX_ID):
            raise RepairError(422, "validation", "Choose a valid technician filter.")
        technician_id = int(technician)
    pattern = search_pattern(query)
    with authorized(workspace, access) as (db, principal, _, can_write):
        if queue == "pickup" and picked_up is True:
            return RepairPage([], None, 0), can_write
        rows, total = repairs.list_page(
            db, principal.shop_id, before, limit + 1, pattern, stage,
            picked_up=False if queue == "pickup" else picked_up,
            archived=archived, paid=paid, technician=principal.user_id if technician == "me" else technician_id,
            unassigned=technician == "unassigned", customer_id=customer_id, queue_stages=REPAIR_QUEUES.get(queue, ()),
            phone_pattern=phone_search_pattern(query),
        )
    page = rows[:limit]
    return RepairPage(page, page[-1].id if len(rows) > limit else None, total), can_write


def list_filter_technicians(workspace: Path, access: RepairAccess):
    with authorized(workspace, access) as (db, principal, _, _):
        return repairs.list_filter_technicians(db, principal.shop_id)


def device_history(workspace: Path, access: RepairAccess, serial: str) -> list[DeviceRepair]:
    with authorized(workspace, access, resource="repairs") as (db, principal, _, _):
        serial = normalize_fields({"serial": serial})["serial"]
        return repairs.for_serial(db, principal.shop_id, serial) if serial else []


def read_repair(workspace: Path, access: RepairAccess, repair_id: int, *, customer_context: bool = False):
    with authorized(workspace, access) as (db, principal, _, can_write):
        record = find_repair(db, principal.shop_id, repair_id)
        names = repairs.relationship_names(db, principal.shop_id, record)
        if customer_context:
            names = {**names, "other_open_repairs": repairs.count_other_open_repairs(db, principal.shop_id, record),
                     "payment_paid": repairs.payment_status(db, principal.shop_id, record.id)}
        return record, names, can_write


def warranty_links(workspace: Path, access: RepairAccess, record: Repair):
    """Return the cited earlier repair and the repairs citing this one, each in one shop-scoped query."""
    with authorized(workspace, access, resource="repairs") as (db, principal, _, _):
        origin = (repairs.find(db, principal.shop_id, record.warranty_repair_id)
                  if record.warranty_repair_id else None)
        return origin, repairs.warranty_returns(db, principal.shop_id, record.id)


def repair_activity(workspace: Path, access: RepairAccess, repair_id: int, *,
                    before: int | None = None, limit: int = 25) -> RepairActivityPage:
    with authorized(workspace, access, resource="repairs") as (db, principal, _, _):
        if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
            raise RepairError(422, "validation", "Invalid repair page.")
        find_repair(db, principal.shop_id, repair_id)
        rows = repairs.list_activity(db, principal.shop_id, repair_id, before, limit + 1)
    return RepairActivityPage(rows[:limit], rows[limit - 1].id if len(rows) > limit else None)


def repair_options(workspace: Path, access: RepairAccess, repair_id: int | None = None, *,
                   customer_id: int | None = None):
    with authorized(workspace, access, resource="repairs") as (db, principal, _, can_write):
        if not can_write:
            raise RepairError(403, "forbidden", "Your account or token cannot change repairs.")
        shop_id = principal.shop_id
        current = None if repair_id is None else find_repair(db, shop_id, repair_id)
        selected_customer = (repairs.customer_name(db, shop_id, customer_id, current.customer_id if current else None)
                             if customer_id is not None and 1 <= customer_id <= MAX_ID else None)
        customers = repairs.customer_options(db, shop_id, current.customer_id, customer_id
                                             if selected_customer else None) if current else []
        return selected_customer, customers


def update_repair(workspace: Path, access: RepairAccess, repair_id: int, patch: RepairPatch,
                  *, key: str, preserve_technician: bool = False) -> Repair:
    values = normalized_input(RepairInput(**{name: getattr(patch, name) for name in asdict(patch)
                                             if name != "revision"}))
    if not 1 <= patch.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed repair revision.")
    operation = "repair.update"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"id": repair_id, "revision": patch.revision, "fields": repair_fields(values)}
        if preserve_technician:
            payload["preserve_technician"] = True
        fingerprint, previous = replay(db, principal, operation, key, payload, Repair)
        if previous is not None:
            return previous
        current = find_unarchived_repair(db, principal.shop_id, repair_id)
        if current.revision != patch.revision:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        until = current.warranty_until if values.warranty_until is None else values.warranty_until or None
        link = current.warranty_repair_id if values.warranty_repair_id is None else values.warranty_repair_id
        values = replace(values, warranty_until=until, warranty_repair_id=link,
                         technician_id=current.technician_id if preserve_technician else values.technician_id)
        check_relationships(db, principal.shop_id, values, current=current)
        changes = {name: {"before": getattr(current, name), "after": value}
                   for name, value in asdict(values).items() if value != getattr(current, name)}
        if not changes:
            raise RepairError(422, "validation", "Change at least one repair field.")
        updated = repairs.update(db, principal.shop_id, repair_id, patch.revision, values)
        if updated is None:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        repairs.save_result(db, principal.shop_id, principal.user_id, operation, key,
                            fingerprint, repair_id, updated)
        repairs.audit(db, principal, access.source, token_label, operation, "repair", repair_id, changes)
        return updated


def stage_options(stage: str) -> list[str]:
    return sorted(STAGE_TRANSITIONS[stage])


def change_stage(workspace: Path, access: RepairAccess, repair_id: int,
                 change: StageChange, *, key: str) -> Repair:
    return change_ticket_status(workspace, access, repair_id, revision=change.revision,
                                stage=change.stage, reason=change.reason, key=key)


def change_ticket_status(workspace: Path, access: RepairAccess, repair_id: int,
                         *, revision: int, stage: str, reason: str, key: str,
                         pickup: bool | None = None, payment: bool | None = None) -> Repair:
    if not 1 <= revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed repair revision.")
    reason = reason.strip().replace("\r\n", "\n")
    if len(reason) > 1000 or any(not char.isprintable() and char not in "\n\t" for char in reason):
        raise RepairError(422, "validation", "Use at most 1000 printable characters for the reason.",
                          {"reason": "Enter at most 1000 supported characters."})
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"id": repair_id, "revision": revision,
                   "stage": stage, "reason": reason}
        if pickup is not None:
            payload["pickup"] = pickup
        if payment is not None:
            payload["payment"] = payment
        fingerprint, previous = replay(db, principal, "repair.update", key, payload, Repair)
        if previous is not None:
            return previous
        stage_requested = bool(stage) or (pickup is None and payment is None)
        find = find_unarchived_repair if stage_requested else find_repair
        current = find(db, principal.shop_id, repair_id)
        if current.revision != revision:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        if pickup is not None or payment is not None:
            if current.stage not in TERMINAL_REPAIR_STAGES:
                raise RepairError(409, "finish_repair", "Finish the repair before changing pickup or payment status.")
            if stage and stage not in TERMINAL_REPAIR_STAGES:
                raise RepairError(409, "finish_repair", "Reopen the repair separately from pickup or payment changes.")
        updated = current
        was_picked_up = current.picked_up_at is not None
        if pickup is not None and pickup == was_picked_up:
            raise RepairError(409, "same_state", "The repair already has this state.")
        prior_payment = repairs.payment_status(db, principal.shop_id, repair_id) if payment is not None else None
        if payment is not None and payment == prior_payment:
            raise RepairError(409, "same_state", "The repair already has this state.")
        if stage_requested:
            if current.stage == stage:
                raise RepairError(409, "same_stage", "The repair is already in this stage.")
            reopening = current.stage in TERMINAL_REPAIR_STAGES and stage == "in_progress"
            if stage not in STAGE_TRANSITIONS[current.stage]:
                source, target = (value.replace("_", " ").title() for value in (current.stage, stage))
                raise RepairError(409, "invalid_transition", f"Cannot move from {source} to {target}.")
            # The ticket does not require a note; integrations keep their existing contract.
            if reopening and not reason and access.source != "web":
                raise RepairError(422, "validation", "Reopening requires an explicit reason.",
                                  {"reason": "Explain why the completed repair is being reopened."})
            if reason and not reopening:
                raise RepairError(422, "validation", "A reason is used only when reopening a repair.",
                                  {"reason": "Leave this blank for an ordinary stage change."})
            updated = repairs.update_stage(db, principal.shop_id, repair_id, revision, stage)
            if updated is None:
                raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
            changes = {"stage": {"before": current.stage, "after": stage}}
            if reason:
                changes["reopen_reason"] = reason
            repairs.audit(db, principal, access.source, token_label, "repair.stage", "repair",
                          repair_id, changes)
        if pickup is not None:
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            updated = repairs.update_flag(db, principal.shop_id, repair_id, updated.revision,
                                          "picked_up_at", pickup, timestamp)
            if updated is None:
                raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
            repairs.audit(db, principal, access.source, token_label, "repair.pickup", "repair",
                          repair_id, {"pickup": {"before": was_picked_up, "after": pickup}})
        if payment is not None:
            updated = repairs.update_payment(db, principal.shop_id, repair_id, updated.revision, payment)
            if updated is None:
                raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
            repairs.audit(db, principal, access.source, token_label, "repair.payment", "repair",
                          repair_id, {"payment": {"before": prior_payment, "after": payment}})
        repairs.save_result(db, principal.shop_id, principal.user_id, "repair.update", key,
                            fingerprint, repair_id, updated)
        return updated


def change_repair_flag(workspace: Path, access: RepairAccess, repair_id: int,
                       change: RepairFlagChange, *, key: str, flag: str) -> Repair:
    if flag == "payment":  # Same rules and transaction as the ticket's status controls.
        return change_ticket_status(workspace, access, repair_id, revision=change.revision, stage="",
                                    reason="", key=key, payment=change.enabled)
    if flag not in {"pickup", "archive"} or not 1 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply a valid reviewed repair revision.")
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        current = find_repair(db, principal.shop_id, repair_id)
        payload = {"id": repair_id, "revision": change.revision, flag: change.enabled}
        fingerprint, previous = replay(db, principal, "repair.update", key, payload, Repair)
        if previous is not None:
            return previous
        if current.revision != change.revision:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        field = "picked_up_at" if flag == "pickup" else "archived_at"
        enabled = getattr(current, field) is not None
        if enabled == change.enabled:
            raise RepairError(409, "same_state", "The repair already has this state.")
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        updated = repairs.update_flag(db, principal.shop_id, repair_id, change.revision,
                                      field, change.enabled, timestamp)
        if updated is None:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        repairs.save_result(db, principal.shop_id, principal.user_id, "repair.update", key,
                            fingerprint, repair_id, updated)
        repairs.audit(db, principal, access.source, token_label, f"repair.{flag}", "repair",
                      repair_id, {flag: {"before": enabled, "after": change.enabled}})
        return updated


def list_work_entries(workspace: Path, access: RepairAccess, repair_id: int, *,
                      before: int | None = None, limit: int = 25):
    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= MAX_ID):
        raise RepairError(422, "validation", "Invalid work-entry page.")
    with authorized(workspace, access) as (db, principal, _, can_write):
        find_repair(db, principal.shop_id, repair_id)
        rows = repairs.list_entries(db, principal.shop_id, repair_id, before, limit + 1)
    page = rows[:limit]
    return WorkEntryPage(page, page[-1].id if len(rows) > limit else None), can_write


def normalize_entry(kind: str, body: str) -> tuple[str, str]:
    body = body.strip().replace("\r\n", "\n")
    errors = {}
    if kind not in ENTRY_KINDS:
        errors["kind"] = "Choose a valid entry type."
    if not body:
        errors["body"] = "Enter work-entry text."
    elif len(body) > 5000:
        errors["body"] = "Use at most 5000 characters."
    elif any(not char.isprintable() and char not in "\n\t" for char in body):
        errors["body"] = "Remove unsupported characters."
    if errors:
        raise RepairError(422, "validation", "Check the work entry.", errors)
    return kind, body


def normalize_reason(reason: str) -> str:
    reason = reason.strip().replace("\r\n", "\n")
    if not reason or len(reason) > 1000 or any(
        not char.isprintable() and char not in "\n\t" for char in reason
    ):
        raise RepairError(422, "validation", "Check the change reason.",
                          {"reason": "Enter a reason of 1 to 1000 supported characters."})
    return reason


def create_work_entry(workspace: Path, access: RepairAccess, repair_id: int,
                      details: WorkEntryInput, *, key: str) -> WorkEntry:
    kind, body = normalize_entry(details.kind, details.body)
    operation = "work_entry.create"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"repair_id": repair_id, "kind": kind, "body": body}
        fingerprint, previous = replay(db, principal, operation, key, payload, WorkEntry)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        created = repairs.insert_entry(db, principal, repair_id, kind, body)
        repairs.save_result(db, principal.shop_id, principal.user_id, operation, key,
                            fingerprint, repair_id, created)
        repairs.audit(db, principal, access.source, token_label, operation, "work_entry", created.id,
                      {"repair_id": repair_id, "kind": kind})
        return created


def find_work_entry(db, shop_id: int, repair_id: int, entry_id: int) -> WorkEntry:
    find_repair(db, shop_id, repair_id)
    entry = repairs.find_entry(db, shop_id, repair_id, entry_id) if 1 <= entry_id <= MAX_ID else None
    if entry is None:
        raise RepairError(404, "not_found", "Work entry not found.")
    return entry


def read_work_entry(workspace: Path, access: RepairAccess, repair_id: int, entry_id: int):
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        entry = find_work_entry(db, principal.shop_id, repair_id, entry_id)
        return entry, can_write and repair.archived_at is None


def correct_work_entry(workspace: Path, access: RepairAccess, repair_id: int, entry_id: int,
                       correction: WorkEntryCorrection, *, key: str) -> WorkEntry:
    kind, body = normalize_entry(correction.kind, correction.body)
    reason = normalize_reason(correction.reason)
    if not 1 <= correction.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed work-entry revision.")
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"entry_id": entry_id, "revision": correction.revision,
                   "kind": kind, "body": body, "reason": reason}
        fingerprint, previous = replay(db, principal, "work_entry.create", key, payload, WorkEntry)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        current = find_work_entry(db, principal.shop_id, repair_id, entry_id)
        if current.deleted:
            raise RepairError(409, "deleted", "Restore the work entry before correcting it.")
        if current.revision != correction.revision:
            raise RepairError(409, "stale_revision", "The work entry changed. Review its history.")
        if (kind, body) == (current.kind, current.body):
            raise RepairError(422, "validation", "Change the entry type or text before correcting it.")
        updated = repairs.update_entry(db, principal, current, kind=kind, body=body,
                                       reason=reason, deleted=False, timestamp=None)
        if updated is None:
            raise RepairError(409, "stale_revision", "The work entry changed. Review its history.")
        repairs.save_result(db, principal.shop_id, principal.user_id, "work_entry.create", key,
                            fingerprint, repair_id, updated)
        repairs.audit(db, principal, access.source, token_label, "work_entry.correct",
                      "work_entry", entry_id, {"repair_id": repair_id, "reason": reason})
        return updated


def change_entry_visibility(workspace: Path, access: RepairAccess, repair_id: int, entry_id: int,
                            change: WorkEntryVisibility, *, key: str) -> WorkEntry:
    reason = normalize_reason(change.reason)
    if not 1 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed work-entry revision.")
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"entry_id": entry_id, "revision": change.revision,
                   "deleted": change.deleted, "reason": reason}
        fingerprint, previous = replay(db, principal, "work_entry.create", key, payload, WorkEntry)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        current = find_work_entry(db, principal.shop_id, repair_id, entry_id)
        if current.revision != change.revision:
            raise RepairError(409, "stale_revision", "The work entry changed. Review its history.")
        if current.deleted == change.deleted:
            raise RepairError(409, "same_state", "The work entry already has this visibility.")
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        updated = repairs.update_entry(db, principal, current, kind=current.kind, body=current.body,
                                       reason=reason, deleted=change.deleted, timestamp=timestamp)
        if updated is None:
            raise RepairError(409, "stale_revision", "The work entry changed. Review its history.")
        repairs.save_result(db, principal.shop_id, principal.user_id, "work_entry.create", key,
                            fingerprint, repair_id, updated)
        operation = "work_entry.delete" if change.deleted else "work_entry.restore"
        repairs.audit(db, principal, access.source, token_label, operation, "work_entry", entry_id,
                      {"repair_id": repair_id, "reason": reason})
        return updated


def work_entry_history(workspace: Path, access: RepairAccess, repair_id: int, entry_id: int):
    with authorized(workspace, access) as (db, principal, _, _):
        find_work_entry(db, principal.shop_id, repair_id, entry_id)
        return repairs.entry_history(db, principal.shop_id, repair_id, entry_id)
