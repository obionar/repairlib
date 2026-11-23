"""Customer privacy for AI agent callers: names become `C<id>`, phone, email and address `[masked]`.

Applied to results on their way out (agent API tokens, MCP, the CLI under `REPAIRLIB_AGENT=1`);
stored rows and domain results are untouched. Search still matches the real name server-side.
It keeps personal data out of the model's context in normal use; it is not a security boundary
against a caller who can read the workspace files.
"""

from dataclasses import fields, is_dataclass, replace

from repairlib.contracts import (
    CustomerExport, CustomerHistoryEvent, CustomerHistoryPage, CustomerPage, Dashboard, QueueRepair,
    RepairBrief, RepairListRow, RepairPage,
)

CONTACT_FIELDS = ("phone", "email", "address")
MASK = "[masked]"


def customer_ref(customer_id: int | None) -> str:
    """The customer reference agents and staff share: `C7`."""
    return f"C{customer_id}" if customer_id else ""


def is_customer(value) -> bool:
    return is_dataclass(value) and {"id", "name", "phone", "email"} <= {f.name for f in fields(value)}


def private_customer(customer):
    return replace(customer, name=customer_ref(customer.id),
                   **{name: MASK for name in CONTACT_FIELDS if getattr(customer, name)})


def private_event(event: CustomerHistoryEvent) -> CustomerHistoryEvent:
    changes = {}
    for name, change in event.changes.items():
        if name in ("name", *CONTACT_FIELDS) and isinstance(change, dict):
            change = {side: MASK if value else value for side, value in change.items()}
        changes[name] = change
    return replace(event, changes=changes)


def private_names(customer_id: int | None, names: dict) -> dict:
    """A repair's relationship names (see storage.repairs.relationship_names) without the person."""
    return {**names, "customer": customer_ref(customer_id),
            **{key: MASK for key in ("customer_phone", "customer_email") if names.get(key)}}


def private_result(value):
    """Hide customer identity in any result shape that carries it; anything else is returned as it is."""
    if isinstance(value, (CustomerPage, CustomerHistoryPage, RepairPage)):
        return replace(value, items=[private_result(item) for item in value.items])
    if isinstance(value, CustomerHistoryEvent):
        return private_event(value)
    if isinstance(value, (RepairListRow, QueueRepair)):
        return replace(value, customer_name=customer_ref(value.customer_id) or None)
    if isinstance(value, RepairBrief):
        return replace(value, customer=customer_ref(value.repair.customer_id),
                       customer_phone=MASK if value.customer_phone else "")
    if isinstance(value, Dashboard):
        return replace(value, open_work=[private_result(row) for row in value.open_work],
                       awaiting_pickup=[private_result(row) for row in value.awaiting_pickup],
                       recent_customers=[private_customer(row) for row in value.recent_customers])
    if isinstance(value, CustomerExport):
        return replace(value, customer=private_customer(value.customer))
    if isinstance(value, (list, tuple)):
        return type(value)(private_result(item) for item in value)
    return private_customer(value) if is_customer(value) else value
