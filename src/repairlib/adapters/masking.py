"""Contact-data masking for agent callers: agent API tokens and every MCP result.

The stored rows and the domain results are untouched; a person's token, a browser session and
the local CLI see the data whole. Only phone, email and address are masked: names, notes, ids,
revisions, repairs and money pass through.
"""

from dataclasses import replace
import logging
from pathlib import Path

from repairlib.contracts import (
    CustomerExport, CustomerHistoryEvent, CustomerHistoryPage, CustomerPage, Dashboard, RepairBrief,
)
from repairlib.domain.customers import CustomerAccess
from repairlib.domain.tokens import authenticate_api_token

CONTACT_FIELDS = ("phone", "email", "address")
MASK = "[masked]"


def is_agent(workspace: Path, access: CustomerAccess) -> bool:
    if access.source != "api":
        return False
    try:
        identity = authenticate_api_token(workspace, access.credential)
    except Exception:
        logging.getLogger(__name__).warning("Customer response identity lookup failed; masking contacts")
        return True
    # A lost identity may follow a committed write: mask rather than claim it failed.
    return identity is None or identity.kind == "agent"


def mask_customer(customer):
    return replace(customer, **{name: MASK for name in CONTACT_FIELDS if getattr(customer, name)})


def mask_event(event: CustomerHistoryEvent) -> CustomerHistoryEvent:
    changes = {}
    for name, change in event.changes.items():
        if name in CONTACT_FIELDS and isinstance(change, dict):
            change = {side: MASK if value else value for side, value in change.items()}
        changes[name] = change
    return replace(event, changes=changes)


def mask_result(value):
    """Mask a customer, page, history page, export, repair brief or dashboard; anything else as it is."""
    if isinstance(value, CustomerPage):
        return replace(value, items=[mask_customer(row) for row in value.items])
    if isinstance(value, CustomerHistoryPage):
        return replace(value, items=[mask_event(event) for event in value.items])
    if isinstance(value, CustomerExport):
        return replace(value, customer=mask_customer(value.customer))
    if isinstance(value, RepairBrief):
        return replace(value, customer_phone=MASK if value.customer_phone else "")
    if isinstance(value, Dashboard):
        return replace(value, recent_customers=[mask_customer(row) for row in value.recent_customers])
    return mask_customer(value) if hasattr(value, "phone") and hasattr(value, "email") else value
