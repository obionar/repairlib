"""Parts/labor charges, repair discount and deterministic totals."""

from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

from repairlib import money
from repairlib.contracts import (
    Charge, ChargeInput, ChargePatch, ChargeSummary, ChargeVisibility, DiscountChange,
)
from repairlib.domain.customers import MAX_ID, authorized
from repairlib.domain.replay import replay as replay_receipt
from repairlib.domain.repairs import (
    RepairAccess, RepairError, find_repair, find_unarchived_repair, normalize_entry, normalize_reason,
)
from repairlib.storage import charges, repairs
from repairlib.storage import settings as shop_settings

MAX_ACTIVE_CHARGES = 500  # with field bounds, keeps every total inside SQLite INTEGER
DEFAULT_UNITS = {"part": "pcs", "labor": "h"}


def invalid(field: str, message: str) -> RepairError:
    return RepairError(422, "validation", "Check the charge fields.", {field: message})


def normalize(details: ChargeInput) -> dict:
    if details.kind not in DEFAULT_UNITS:
        raise invalid("kind", "Choose part or labour.")
    description = details.description.strip()
    if not description or len(description) > 200 or not description.isprintable():
        raise invalid("description", "Enter 1 to 200 printable characters.")
    unit = details.unit.strip() or DEFAULT_UNITS[details.kind]
    if len(unit) > 16 or not unit.isprintable():
        raise invalid("unit", "Use at most 16 printable characters.")
    notes = details.notes.strip().replace("\r\n", "\n")
    if len(notes) > 1000 or any(not c.isprintable() and c not in "\n\t" for c in notes):
        raise invalid("notes", "Use at most 1000 supported characters.")
    for name in ("quantity_milli", "unit_price_cents", "unit_cost_cents"):
        value = getattr(details, name)
        if value is not None and (type(value) is not int):
            raise invalid(name, "Use a whole number.")
    if not 1 <= details.quantity_milli <= money.MAX_MILLI:
        raise invalid("quantity_milli", "Quantity must be from 0.001 to 1000000.")
    if not 0 <= details.unit_price_cents <= money.MAX_CENTS:
        raise invalid("unit_price_cents", "Prices cannot be negative or above 100000000.00.")
    if details.unit_cost_cents is not None and not 0 <= details.unit_cost_cents <= money.MAX_CENTS:
        raise invalid("unit_cost_cents", "Cost cannot be negative or above 100000000.00.")
    return {"kind": details.kind, "description": description, "quantity_milli": details.quantity_milli,
            "unit": unit, "unit_price_cents": details.unit_price_cents,
            "unit_cost_cents": details.unit_cost_cents, "notes": notes}


def visible(charge: Charge, principal) -> Charge:
    return charge if principal.role != "viewer" else replace(charge, unit_cost_cents=None)


def build_summary(db, principal, repair_id: int) -> ChargeSummary:
    items = charges.list_for_repair(db, principal.shop_id, repair_id)
    active = [item for item in items if not item.deleted]
    parts = sum(item.line_total_cents for item in active if item.kind == "part")
    labor = sum(item.line_total_cents for item in active if item.kind == "labor")
    kind, value, revision = charges.discount(db, principal.shop_id, repair_id)
    subtotal = parts + labor
    try:
        reduction = money.discount(kind, value, subtotal)
    except ValueError as error:
        raise RepairError(409, "discount_exceeds_subtotal",
                          f"{error} Change the discount first.") from None
    if subtotal > money.MAX_TOTAL:
        raise RepairError(409, "total_out_of_range", "The repair total would be out of range.")
    return ChargeSummary(
        items=[visible(item, principal) for item in items], parts_subtotal_cents=parts,
        labor_subtotal_cents=labor, subtotal_cents=subtotal, discount_kind=kind,
        discount_value=value, discount_cents=reduction, total_cents=subtotal - reduction,
        discount_revision=revision,
    )


def replay(db, principal, operation: str, key: str, payload: dict, shape):
    return replay_receipt(db, principal, "charge", operation, key, payload, shape, RepairError)


def charge_shape(values: dict) -> Charge:
    return Charge(**values)


def summary_shape(values: dict) -> ChargeSummary:
    return ChargeSummary(**(values | {"items": [Charge(**item) for item in values["items"]]}))


def audit(db, principal, source, token_label, operation, entity_type, entity_id, changes):
    repairs.audit(db, principal, source, token_label, operation, entity_type, entity_id, changes)


def charge_summary(workspace: Path, access: RepairAccess, repair_id: int):
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        return build_summary(db, principal, repair_id), can_write and repair.archived_at is None

def work_log_billing(workspace: Path, access: RepairAccess, repair_id: int):
    with authorized(workspace, access) as (db, principal, _, _):
        find_repair(db, principal.shop_id, repair_id)
        summary = build_summary(db, principal, repair_id)
        manual = charges.manual_part_ids(db, principal.shop_id, repair_id)
        used = [item for item in summary.items if not item.deleted and item.kind == "part"
                and (item.movement_id is not None or item.id in manual)]
        return summary, used


def find_charge(db, shop_id: int, repair_id: int, charge_id: int) -> Charge:
    found = charges.find(db, shop_id, repair_id, charge_id) if 1 <= charge_id <= MAX_ID else None
    if found is None:
        raise RepairError(404, "not_found", "Charge not found.")
    return found


def read_charge(workspace: Path, access: RepairAccess, repair_id: int, charge_id: int):
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        found = find_charge(db, principal.shop_id, repair_id, charge_id)
        return visible(found, principal), can_write and repair.archived_at is None


def append_charge(db, principal, repair_id: int, values: dict) -> Charge:
    """Insert and validate totals inside the caller's write transaction."""
    if charges.active_count(db, principal.shop_id, repair_id) >= MAX_ACTIVE_CHARGES:
        raise RepairError(409, "too_many_charges",
                          f"A repair can have at most {MAX_ACTIVE_CHARGES} active charges.")
    created = charges.insert(db, principal, repair_id, values)
    build_summary(db, principal, repair_id)
    return created


PROTECTED = {"diagnosis": "Diagnosis", "labor": "Labour"}


def ticket_fees(workspace: Path, access: RepairAccess, repair_id: int):
    with authorized(workspace, access) as (db, principal, _, can_write):
        repair = find_repair(db, principal.shop_id, repair_id)
        summary = build_summary(db, principal, repair_id)
        linked = repairs.fee_charge_ids(db, principal.shop_id, repair_id)
        stored = {}
        for key in PROTECTED:
            charge_id = linked.get(key)
            stored[key] = next((item.unit_price_cents for item in summary.items
                                if item.id == charge_id and not item.deleted), 0)
        default = shop_settings.diagnosis_fee(db, principal.shop_id)
        # An explicit zero stays zero; the bench diagnosis is proposed until the technician saves.
        proposed = {"diagnosis": stored["diagnosis"] if "diagnosis" in linked else default,
                    "labor": stored["labor"] if "labor" in linked else 0}
        other = summary.subtotal_cents - stored["diagnosis"] - stored["labor"]
        return {"diagnosis": stored["diagnosis"],
                "diagnosis_input": proposed["diagnosis"],
                "labor": stored["labor"],
                "labor_input": proposed["labor"],
                "parts": summary.parts_subtotal_cents,
                "total_cents": summary.total_cents,
                "pending_total_cents": sum(proposed.values()) + other - summary.discount_cents,
                "discount_cents": summary.discount_cents,
                "discount_amount": summary.discount_value if summary.discount_kind == "amount" else 0,
                "default_cents": default,
                "revision": repair.revision}, can_write and repair.archived_at is None


def set_fees(workspace: Path, access: RepairAccess, repair_id: int, *, diagnosis: int | None = None,
             labor: int | None = None, discount: int | None, revision: int, key: str) -> None:
    if not 1 <= revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed repair revision.")
    for name, amount in (("diagnosis", diagnosis), ("labor", labor)):
        if amount is not None and (type(amount) is not int or not 0 <= amount <= money.MAX_CENTS):
            raise RepairError(422, "validation", "Check the amounts.",
                              {name: "Enter an amount from 0 to 100000000.00."})
    if discount is not None and (type(discount) is not int or not 0 <= discount <= money.MAX_CENTS):
        raise RepairError(422, "validation", "Check the discount.",
                          {"discount": "Enter an amount from 0 to 100000000.00."})
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"repair_id": repair_id, "revision": revision}
        for name, amount in (("diagnosis", diagnosis), ("labor", labor)):
            if amount is not None:
                payload[name] = amount
        if discount is not None:
            payload["discount"] = discount
        fingerprint, previous = replay_receipt(db, principal, "repair", "repair.update", key, payload,
                                               lambda _: True, RepairError)
        if previous is not None:
            return
        current = find_unarchived_repair(db, principal.shop_id, repair_id)
        if current.revision != revision:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        changes = {}
        charged = repairs.fee_charge_ids(db, principal.shop_id, repair_id)
        for name, amount in (("diagnosis", diagnosis), ("labor", labor)):
            if amount is None:
                continue
            values = normalize(ChargeInput(kind="labor", description=PROTECTED[name],
                                           quantity_milli=1000, unit_price_cents=amount))
            charge_id = charged.get(name)
            existing = (find_charge(db, principal.shop_id, repair_id, charge_id)
                        if charge_id is not None else None)
            if existing is not None and existing.deleted:
                raise RepairError(409, "deleted", "Restore the fee charge before changing it.")
            if existing is None and not amount:
                repairs.save_fee(db, principal.shop_id, repair_id, None, name)
            if existing is None and amount:
                created = append_charge(db, principal, repair_id, values)
                repairs.save_fee(db, principal.shop_id, repair_id, created.id, name)
                changes[name] = {"before": 0, "after": amount, "charge_id": created.id}
            elif existing is not None and existing.unit_price_cents != amount:
                if amount:
                    updated = charges.update(db, principal.shop_id, existing, values)
                else:
                    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    updated = charges.set_deleted(db, principal.shop_id, existing, timestamp)
                if updated is None:
                    raise RepairError(409, "stale_revision", "The charge changed. Review the latest version.")
                changes[name] = {"before": existing.unit_price_cents, "after": amount, "charge_id": existing.id}
                if not amount:
                    repairs.save_fee(db, principal.shop_id, repair_id, None, name)
        if discount is not None:
            before_summary = build_summary(db, principal, repair_id)
            if (before_summary.discount_kind, before_summary.discount_value) != ("amount", discount):
                if discount:
                    try:
                        money.discount("amount", discount, before_summary.subtotal_cents)
                    except ValueError as error:
                        raise RepairError(409, "discount_exceeds_subtotal",
                                          f"{error} Change the discount first.") from None
                if not charges.save_discount(db, principal.shop_id, repair_id, "amount" if discount else "none",
                                             discount, before_summary.discount_revision):
                    raise RepairError(409, "stale_revision", "The discount changed. Review the latest totals.")
                changes["discount"] = {"before": before_summary.discount_value, "after": discount}
        changes = {name: value for name, value in changes.items() if value["before"] != value["after"]}
        if changes:
            repairs.bump_revision(db, principal.shop_id, repair_id)
        summary = build_summary(db, principal, repair_id)
        repairs.save_result(db, principal.shop_id, principal.user_id, "repair.update", key, fingerprint,
                            repair_id, summary)
        repairs.audit(db, principal, access.source, token_label, "repair.fees", "repair", repair_id,
                      {"revision": revision, "fees": changes})


def create_charge(workspace: Path, access: RepairAccess, repair_id: int, details: ChargeInput,
                  *, key: str, part_revision: int | None = None) -> Charge:
    values = normalize(details)
    if part_revision is not None and (details.kind != "part" or not 1 <= part_revision < MAX_ID):
        raise RepairError(422, "validation", "Supply the reviewed repair revision.")
    operation = "charge.create"
    payload = {"repair_id": repair_id, **values}
    if part_revision is not None:
        payload["part_revision"] = part_revision
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        fingerprint, previous = replay(db, principal, operation, key, payload, charge_shape)
        if previous is not None:
            return previous
        repair = find_unarchived_repair(db, principal.shop_id, repair_id)
        if part_revision is not None and repair.revision != part_revision:
            raise RepairError(409, "stale_revision", "The repair changed. Review the latest version.")
        created = append_charge(db, principal, repair_id, values)
        changes = {"repair_id": repair_id, **{k: v for k, v in values.items() if k != "notes"}}
        if part_revision is not None:
            heading = f"Part used: {created.description} × {money.format_milli(created.quantity_milli)} {created.unit}"
            kind, body = normalize_entry("work", heading)
            changes["work_entry_id"] = repairs.insert_entry(db, principal, repair_id, kind, body).id
        charges.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint,
                            repair_id, created)
        audit(db, principal, access.source, token_label, operation, "charge", created.id, changes)
        return created


def correct_charge(workspace: Path, access: RepairAccess, repair_id: int, charge_id: int,
                   patch: ChargePatch, *, key: str) -> Charge:
    values = normalize(ChargeInput(**{item.name: getattr(patch, item.name)
                                      for item in fields(ChargeInput)}))
    reason = normalize_reason(patch.reason)
    if not 1 <= patch.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed charge revision.")
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"charge_id": charge_id, "revision": patch.revision, "reason": reason, **values}
        fingerprint, previous = replay(db, principal, "charge.update", key, payload, charge_shape)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        current = find_charge(db, principal.shop_id, repair_id, charge_id)
        if current.deleted:
            raise RepairError(409, "deleted", "Restore the charge before correcting it.")
        if current.revision != patch.revision:
            raise RepairError(409, "stale_revision", "The charge changed. Review the latest version.")
        if current.movement_id is not None and any(
            values[name] != getattr(current, name) for name in ("kind", "description", "unit", "unit_cost_cents")
        ):
            raise RepairError(409, "inventory_linked", "Catalog snapshots are fixed on inventory-linked charges.")
        changes = {name: {"before": getattr(current, name), "after": value}
                   for name, value in values.items() if getattr(current, name) != value}
        if not changes:
            raise RepairError(422, "validation", "Change at least one charge field.")
        updated = charges.update(db, principal.shop_id, current, values)
        if updated is None:
            raise RepairError(409, "stale_revision", "The charge changed. Review the latest version.")
        build_summary(db, principal, repair_id)
        charges.save_result(db, principal.shop_id, principal.user_id, "charge.update", key,
                            fingerprint, repair_id, updated)
        audit(db, principal, access.source, token_label, "charge.correct", "charge", charge_id,
              {"repair_id": repair_id, "reason": reason, **changes})
        return updated


def change_charge_visibility(workspace: Path, access: RepairAccess, repair_id: int, charge_id: int,
                             change: ChargeVisibility, *, key: str) -> Charge:
    reason = normalize_reason(change.reason)
    validate_charge_revision(change.revision)
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"charge_id": charge_id, "revision": change.revision, "deleted": change.deleted,
                   "reason": reason}
        fingerprint, previous = replay(db, principal, "charge.update", key, payload, charge_shape)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        current = find_charge(db, principal.shop_id, repair_id, charge_id)
        updated = apply_charge_visibility(db, principal, current, change.revision, change.deleted)
        build_summary(db, principal, repair_id)
        charges.save_result(db, principal.shop_id, principal.user_id, "charge.update", key,
                            fingerprint, repair_id, updated)
        audit(db, principal, access.source, token_label,
              "charge.delete" if change.deleted else "charge.restore", "charge", charge_id,
              {"repair_id": repair_id, "reason": reason})
        return updated


def validate_charge_revision(revision: int) -> None:
    if type(revision) is not int or not 1 <= revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed charge revision.")


def apply_charge_visibility(db, principal, current: Charge, revision: int, deleted: bool) -> Charge:
    """Update visibility inside the caller's transaction. The caller validates totals."""
    if current.revision != revision:
        raise RepairError(409, "stale_revision", "The charge changed. Review the latest version.")
    if current.deleted == deleted:
        raise RepairError(409, "same_state", "The charge already has this state.")
    if not deleted and charges.active_count(db, principal.shop_id, current.repair_id) >= MAX_ACTIVE_CHARGES:
        raise RepairError(409, "too_many_charges",
                          f"A repair can have at most {MAX_ACTIVE_CHARGES} active charges.")
    timestamp = (datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                 if deleted else None)
    updated = charges.set_deleted(db, principal.shop_id, current, timestamp)
    if updated is None:
        raise RepairError(409, "stale_revision", "The charge changed. Review the latest version.")
    return updated


def set_discount(workspace: Path, access: RepairAccess, repair_id: int, change: DiscountChange,
                 *, key: str) -> ChargeSummary:
    if not 0 <= change.revision < MAX_ID:
        raise RepairError(422, "validation", "Supply the reviewed discount revision.")
    if type(change.value) is not int:
        raise RepairError(422, "validation", "Check the discount.", {"value": "Use a whole number."})
    operation = "repair.discount"
    with authorized(workspace, access, write=True) as (db, principal, token_label, _):
        payload = {"repair_id": repair_id, "revision": change.revision, "kind": change.kind,
                   "value": change.value}
        fingerprint, previous = replay(db, principal, operation, key, payload, summary_shape)
        if previous is not None:
            return previous
        find_unarchived_repair(db, principal.shop_id, repair_id)
        before = build_summary(db, principal, repair_id)
        if before.discount_revision != change.revision:
            raise RepairError(409, "stale_revision", "The discount changed. Review the latest totals.")
        try:
            money.discount(change.kind, change.value, before.subtotal_cents)
        except ValueError as error:
            raise RepairError(422, "validation", "Check the discount.", {"value": str(error)}) from None
        if (change.kind, change.value) == (before.discount_kind, before.discount_value):
            raise RepairError(409, "same_state", "The repair already has this discount.")
        if not charges.save_discount(db, principal.shop_id, repair_id, change.kind, change.value,
                                     change.revision):
            raise RepairError(409, "stale_revision", "The discount changed. Review the latest totals.")
        after = build_summary(db, principal, repair_id)
        charges.save_result(db, principal.shop_id, principal.user_id, operation, key, fingerprint,
                            repair_id, after)
        audit(db, principal, access.source, token_label, operation, "repair_discount", repair_id, {
            "kind": {"before": before.discount_kind, "after": change.kind},
            "value": {"before": before.discount_value, "after": change.value},
        })
        return after
