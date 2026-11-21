"""Inventory catalog JSON resources (integer cents and milli-units)."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import TypeAdapter

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, page_number, json_body
from repairlib.contracts import (
    InventoryItem, InventoryPage, ItemArchive, ItemInput, ItemPatch, MovementPage, StockAdjustment,
    StockChange, StockReceipt, StockReconciliation, StockReversal,
)
from repairlib.domain import inventory, stock
from repairlib.domain.customers import CustomerAccess

INPUT = TypeAdapter(ItemInput)
PATCH = TypeAdapter(ItemPatch)
ARCHIVE = TypeAdapter(ItemArchive)
RECEIPT = TypeAdapter(StockReceipt)
ADJUSTMENT = TypeAdapter(StockAdjustment)
REVERSAL = TypeAdapter(StockReversal)


input_json = json_body(INPUT, "inventory item")
patch_json = json_body(PATCH, "inventory item edit")
archive_json = json_body(ARCHIVE, "inventory archive change")
receipt_json = json_body(RECEIPT, "stock receipt")
adjustment_json = json_body(ADJUSTMENT, "stock adjustment")
reversal_json = json_body(REVERSAL, "stock reversal")


def inventory_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/inventory")

    @router.get("", response_model=InventoryPage)
    def index(access: Annotated[CustomerAccess, Depends(access_dependency)],
              before: str | None = None, limit: str | None = None, include_archived: bool = False,
              low_stock: bool = False, q: str = "", category: str = ""):
        return inventory.list_items(workspace, access, before=page_number(before, noun="inventory"),
                                    limit=page_number(limit, 25, "inventory"), include_archived=include_archived,
                                    low_stock=low_stock, query=q, category=category)

    @router.post("", response_model=InventoryItem, status_code=201, openapi_extra=body_schema(INPUT))
    def create(access: Annotated[CustomerAccess, Depends(access_dependency)],
               details: Annotated[ItemInput, Depends(input_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return inventory.create_item(workspace, access, details, key=key)

    @router.get("/reconciliation", response_model=StockReconciliation)
    def reconciliation(access: Annotated[CustomerAccess, Depends(access_dependency)]):
        return stock.reconcile_stock(workspace, access)

    @router.get("/{item_id}", response_model=InventoryItem)
    def detail(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        return inventory.read_item(workspace, access, item_id)

    @router.patch("/{item_id}", response_model=InventoryItem, openapi_extra=body_schema(PATCH))
    def edit(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
             patch: Annotated[ItemPatch, Depends(patch_json)],
             key: Annotated[str, Depends(idempotency_key)]):
        return inventory.edit_item(workspace, access, item_id, patch, revision=patch.revision, key=key)

    @router.post("/{item_id}/archive", response_model=InventoryItem, openapi_extra=body_schema(ARCHIVE))
    def archive(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                change: Annotated[ItemArchive, Depends(archive_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return inventory.archive_item(workspace, access, item_id, revision=change.revision, key=key)

    @router.post("/{item_id}/restore", response_model=InventoryItem, openapi_extra=body_schema(ARCHIVE))
    def restore(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                change: Annotated[ItemArchive, Depends(archive_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return inventory.restore_item(workspace, access, item_id, revision=change.revision, key=key)

    @router.get("/{item_id}/movements", response_model=MovementPage)
    def movements(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                  before: str | None = None, limit: str | None = None):
        return stock.list_movements(workspace, access, item_id, before=page_number(before, noun="stock movement"),
                                    limit=page_number(limit, 25, "stock movement"))

    @router.post("/{item_id}/receive", response_model=StockChange, status_code=201,
                 openapi_extra=body_schema(RECEIPT))
    def receive(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                receipt: Annotated[StockReceipt, Depends(receipt_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return stock.receive_stock(workspace, access, item_id, receipt.quantity_milli,
                                   revision=receipt.revision, key=key, reason=receipt.reason)

    @router.post("/{item_id}/adjust", response_model=StockChange, status_code=201,
                 openapi_extra=body_schema(ADJUSTMENT))
    def adjust(item_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
               adjustment: Annotated[StockAdjustment, Depends(adjustment_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return stock.adjust_stock(workspace, access, item_id, adjustment.quantity_milli, adjustment.reason,
                                  revision=adjustment.revision, key=key)

    @router.post("/{item_id}/movements/{movement_id}/reverse", response_model=StockChange, status_code=201,
                 openapi_extra=body_schema(REVERSAL))
    def reverse(item_id: int, movement_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                reversal: Annotated[StockReversal, Depends(reversal_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return stock.reverse_movement(workspace, access, item_id, movement_id, reversal.reason,
                                      revision=reversal.revision, key=key)

    return router
