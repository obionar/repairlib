"""Raw ZIP transport for portable shop export, preview and import."""

from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.attachments import UPLOAD_BODY
from repairlib.adapters.api.customers import idempotency_key
from repairlib.contracts import PORTABLE_MAX_BYTES, PortableImport, PortablePreview
from repairlib.domain import portable_shop
from repairlib.domain.customers import CustomerAccess, CustomerError

ZIP = "application/zip"
CHUNK = 1 << 20


def refuse_query(request: Request) -> None:
    if request.query_params:
        raise CustomerError(422, "validation", "Portable archive requests accept no query parameters.")


async def receive_archive(request: Request, operation, workspace: Path, access: CustomerAccess, **options):
    lengths = request.headers.getlist("content-length")
    if lengths:
        length = lengths[0]
        if len(lengths) != 1 or not length.isascii() or not length.isdecimal():
            raise CustomerError(422, "validation", "Invalid portable archive content length.")
        digits = length.lstrip("0")
        if len(digits) > 10 or int(digits or "0") > PORTABLE_MAX_BYTES:
            raise CustomerError(413, "archive_too_large", "The portable archive exceeds its size limit.")
    try:
        with TemporaryFile() as handle:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > PORTABLE_MAX_BYTES:
                    raise CustomerError(413, "archive_too_large",
                                        "The portable archive exceeds its size limit.")
                if chunk:
                    await run_in_threadpool(handle.write, chunk)
            return await run_in_threadpool(operation, workspace, access, handle, **options)
    except OSError as error:
        raise CustomerError(503, "unavailable", "Could not stage or read the portable archive.") from error


def portable_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/portable")

    @router.get("/export", response_class=StreamingResponse,
                responses={200: {"content": {ZIP: {"schema": {"type": "string", "format": "binary"}}}}})
    def export(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        if request.query_params:
            portable_shop.authorize_preview(workspace, access)
            refuse_query(request)
        handle, size = portable_shop.export_archive(workspace, access)

        def chunks():
            try:
                while chunk := handle.read(CHUNK):
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(chunks(), media_type=ZIP, background=BackgroundTask(handle.close),
                                 headers={"Content-Length": str(size),
                                          "Content-Disposition": 'attachment; filename="repairlib-shop.zip"',
                                          "Cross-Origin-Resource-Policy": "same-origin"})

    @router.post("/preview", response_model=PortablePreview, openapi_extra=UPLOAD_BODY)
    async def preview(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)]):
        reviewed_shop = await run_in_threadpool(portable_shop.authorize_preview, workspace, access)
        refuse_query(request)
        return await receive_archive(request, portable_shop.preview_archive, workspace, access,
                                     reviewed_shop=reviewed_shop)

    @router.post("/import", response_model=PortableImport, status_code=201, openapi_extra=UPLOAD_BODY)
    async def import_shop(request: Request, access: Annotated[CustomerAccess, Depends(access_dependency)],
                          key: Annotated[str, Depends(idempotency_key)],
                          destination_shop_id: Annotated[int, Query(ge=1, le=(1 << 63) - 1)],
                          revision: Annotated[int, Query(ge=1, lt=(1 << 63) - 1)]):
        await run_in_threadpool(portable_shop.authorize_import, workspace, access, destination_shop_id)
        if sorted(name for name, _ in request.query_params.multi_items()) != ["destination_shop_id", "revision"]:
            raise CustomerError(422, "validation", "Supply one destination shop ID and one reviewed revision.")
        return await receive_archive(request, portable_shop.import_archive, workspace, access,
                                     destination_shop_id=destination_shop_id, revision=revision, key=key)

    return router
