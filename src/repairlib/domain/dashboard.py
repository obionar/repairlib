"""Operational dashboard read model."""

from pathlib import Path

from repairlib.contracts import Dashboard, OPEN_REPAIR_STAGES, TERMINAL_REPAIR_STAGES
from repairlib.domain.customers import CustomerAccess, authorized
from repairlib.domain.inventory import visible
from repairlib.storage import customers, dashboard, inventory

REPAIR_PREVIEW_LIMIT = 8
CUSTOMER_PREVIEW_LIMIT = 5
LOW_STOCK_PREVIEW_LIMIT = 8


def read_dashboard(workspace: Path, access: CustomerAccess) -> Dashboard:
    with authorized(workspace, access) as (db, principal, _, _):
        open_work, open_count = dashboard.repair_queue(
            db, principal.shop_id, OPEN_REPAIR_STAGES, awaiting_pickup=False, limit=REPAIR_PREVIEW_LIMIT,
        )
        ready, pickup_count = dashboard.repair_queue(
            db, principal.shop_id, TERMINAL_REPAIR_STAGES,
            awaiting_pickup=True, limit=REPAIR_PREVIEW_LIMIT,
        )
        recent = customers.list_page(db, principal.shop_id, None, CUSTOMER_PREVIEW_LIMIT)
        low_stock, low_stock_count = inventory.low_stock_preview(db, principal.shop_id, LOW_STOCK_PREVIEW_LIMIT)
        return Dashboard(open_work, open_count, ready, pickup_count, recent,
                         [visible(item, principal) for item in low_stock], low_stock_count)
