"""Customer JSON resources under the versioned API."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import TypeAdapter, ValidationError

from repairlib.adapters.body import bounded_body, API_PREFIX
from repairlib.adapters.masking import shown as shown_to
from repairlib.contracts import (
    CustomerAnonymize, CustomerArchiveChange, CustomerExport, CustomerHistoryPage, CustomerInput, CustomerPage, CustomerPatch, CustomerResponse,
)
from repairlib.domain.customer_privacy import anonymize_customer, export_customer
from repairlib.domain.customers import (
    CustomerAccess, CustomerError, change_customer_archive, create_customer, list_customers,
    read_customer, read_customer_history, update_customer,
)

INPUT = TypeAdapter(CustomerInput)
PATCH = TypeAdapter(CustomerPatch)
ARCHIVE = TypeAdapter(CustomerArchiveChange)
ANONYMIZE = TypeAdapter(CustomerAnonymize)


def body_schema(shape: TypeAdapter) -> dict:
    return {"requestBody": {"required": True, "content": {
        "application/json": {"schema": shape.json_schema()},
    }}}


async def read_json(request: Request, shape: TypeAdapter, noun: str = "customer"):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise CustomerError(415, "encoding", "Send an application/json request.")
    try:
        body = await bounded_body(request, 32 * 1024)
    except HTTPException as error:
        raise CustomerError(error.status_code, "request_body", error.detail) from None
    try:
        return shape.validate_json(body, strict=True)
    except ValidationError:
        raise CustomerError(422, "validation", f"Send a valid {noun} object.") from None


def json_body(shape: TypeAdapter, noun: str = "customer"):
    """FastAPI dependency that reads one strict JSON request body of `shape`."""
    async def dependency(request: Request):
        return await read_json(request, shape, noun)
    return dependency


customer_json = json_body(INPUT)
patch_json = json_body(PATCH)
archive_json = json_body(ARCHIVE, "customer archive change")
anonymize_json = json_body(ANONYMIZE, "customer anonymize request")


def idempotency_key(request: Request, key: Annotated[str, Header(alias="Idempotency-Key")]):
    if len(request.headers.getlist("idempotency-key")) > 1:
        raise CustomerError(422, "validation", "Supply one idempotency key.")
    return key


def page_number(raw: str | None, default: int | None = None, noun: str = "customer") -> int | None:
    if raw is None:
        return default
    if len(raw) > 19 or not raw.isascii() or not raw.isdecimal():
        raise CustomerError(422, "validation", f"Invalid {noun} page.")
    return int(raw)


def customer_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/customers")

    def shown(access: CustomerAccess, value):
        return shown_to(workspace, access, value)

    @router.get("", response_model=CustomerPage)
    def index(access: Annotated[CustomerAccess, Depends(access_dependency)],
              before: str | None = None, limit: str | None = None, q: str = "",
              include_archived: bool = False):
        page, _ = list_customers(workspace, access, before=page_number(before),
                                 limit=page_number(limit, 25), query=q, include_archived=include_archived)
        return shown(access, page)

    @router.post("", response_model=CustomerResponse, status_code=201, openapi_extra=body_schema(INPUT))
    def create(access: Annotated[CustomerAccess, Depends(access_dependency)],
               details: Annotated[CustomerInput, Depends(customer_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return shown(access, create_customer(workspace, access, details, key=key))

    @router.get("/{customer_id}", response_model=CustomerResponse)
    def detail(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        customer, _ = read_customer(workspace, access, customer_id)
        return shown(access, customer)

    @router.patch("/{customer_id}", response_model=CustomerResponse, openapi_extra=body_schema(PATCH))
    def edit(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
             changes: Annotated[CustomerPatch, Depends(patch_json)],
             key: Annotated[str, Depends(idempotency_key)]):
        return shown(access, update_customer(workspace, access, customer_id, changes, key=key))

    @router.post("/{customer_id}/archive", response_model=CustomerResponse, openapi_extra=body_schema(ARCHIVE))
    def archive(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                change: Annotated[CustomerArchiveChange, Depends(archive_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return shown(access, change_customer_archive(workspace, access, customer_id, change, key=key))

    @router.post("/{customer_id}/anonymize", response_model=CustomerResponse,
                 openapi_extra=body_schema(ANONYMIZE))
    def anonymize(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                  change: Annotated[CustomerAnonymize, Depends(anonymize_json)],
                  key: Annotated[str, Depends(idempotency_key)]):
        return shown(access, anonymize_customer(workspace, access, customer_id, change, key=key))

    @router.get("/{customer_id}/export", response_model=CustomerExport)
    def export(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        return shown(access, export_customer(workspace, access, customer_id))

    @router.get("/{customer_id}/history", response_model=CustomerHistoryPage)
    def history(customer_id: int, access: Annotated[CustomerAccess, Depends(access_dependency)],
                before: str | None = None, limit: str | None = None):
        _, page, _ = read_customer_history(workspace, access, customer_id, before=page_number(before),
                                           limit=page_number(limit, 25))
        return shown(access, page)

    return router
