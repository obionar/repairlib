"""Which API callers are AI agents; what they may see is decided in `domain.privacy`."""

import logging
from pathlib import Path

from repairlib.domain.customers import CustomerAccess
from repairlib.domain.privacy import private_result
from repairlib.domain.tokens import authenticate_api_token


def is_agent(workspace: Path, access: CustomerAccess) -> bool:
    if access.source != "api":
        return False
    try:
        identity = authenticate_api_token(workspace, access.credential)
    except Exception:
        logging.getLogger(__name__).warning("Customer response identity lookup failed; hiding customer identity")
        return True
    # A lost identity may follow a committed write: mask rather than claim it failed.
    return identity is None or identity.kind == "agent"


def shown(workspace: Path, access: CustomerAccess, value):
    """Agent callers get customer names as `C<id>` and contacts masked; everyone else the whole value."""
    return private_result(value) if is_agent(workspace, access) else value
