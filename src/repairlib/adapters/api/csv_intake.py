"""Bearer-authenticated CSV intake via raw request bodies."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from repairlib.adapters.api.customers import idempotency_key
from repairlib.adapters.body import bounded_body, API_PREFIX
from repairlib.contracts import CSV_MAX_BYTES, CsvApply, CsvPreview
from repairlib.domain import csv_intake
from repairlib.domain.customer_csv import CUSTOMERS
from repairlib.domain.inventory_csv import CATALOG, OPENING_STOCK
from repairlib.domain.customers import CustomerAccess, CustomerError

CSV_BODY = {"requestBody": {"required": True, "content": {
    "text/csv": {"schema": {"type": "string", "format": "binary"}},
}}}


def require_query(request, expected=()):
    if sorted(name for name, _ in request.query_params.multi_items()) != list(expected):
        raise CustomerError(422, "validation", "Check the CSV request parameters.")


async def receive(request):
    try:
        return bytes(await bounded_body(request, CSV_MAX_BYTES))
    except HTTPException as error:
        if error.status_code == 413:
            raise CustomerError(413, "csv_too_large", "The CSV exceeds its size limit.") from None
        raise CustomerError(422, "validation", "Invalid CSV content length.") from None


def csv_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/csv")
    for intake in (CUSTOMERS, CATALOG, OPENING_STOCK):
        router.include_router(intake_routes(workspace, access_dependency, intake))
    return router


def intake_routes(workspace: Path, access_dependency, intake) -> APIRouter:
    router = APIRouter(prefix=f"/{intake.kind}")

    @router.get("/template", response_class=Response,
                responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}})
    def template(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        content = csv_intake.template(workspace, access, intake)
        require_query(request)
        return Response(content, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{intake.kind}.csv"',
        })

    @router.post("/preview", response_model=CsvPreview, openapi_extra=CSV_BODY)
    async def preview(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        await run_in_threadpool(csv_intake.authorize_upload, workspace, access)
        require_query(request)
        raw = await receive(request)
        return await run_in_threadpool(csv_intake.preview, workspace, access, intake, raw)

    @router.post("/apply", response_model=CsvApply, status_code=201, openapi_extra=CSV_BODY)
    async def apply(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)],
                    key: Annotated[str, Depends(idempotency_key)],
                    preview_id: Annotated[str, Query(pattern=r"^[a-f0-9]{32}$", max_length=32)],
                    sha256: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$", max_length=64)]):
        await run_in_threadpool(csv_intake.authorize_upload, workspace, access)
        require_query(request, ("preview_id", "sha256"))
        raw = await receive(request)
        return await run_in_threadpool(csv_intake.apply, workspace, access, intake, raw,
                                       preview_id=preview_id, input_hash=sha256, key=key)

    return router
