"""Repair attachment JSON metadata and raw-byte upload/download resources."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import TypeAdapter
from starlette.concurrency import run_in_threadpool

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, json_body
from repairlib.adapters.api.repairs import page_number
from repairlib.adapters.files import file_response, stream_body
from repairlib.contracts import (
    Attachment, AttachmentCaption, AttachmentDetach, AttachmentLink, AttachmentPage, AttachmentPurge,
)
from repairlib.domain import attachments
from repairlib.domain.repairs import RepairAccess

CAPTION = TypeAdapter(AttachmentCaption)
LINK = TypeAdapter(AttachmentLink)
DETACH = TypeAdapter(AttachmentDetach)
PURGE = TypeAdapter(AttachmentPurge)
UPLOAD_BODY = {"requestBody": {"required": True, "content": {
    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
}}}


caption_json = json_body(CAPTION, "attachment caption change")
link_json = json_body(LINK, "attachment link change")
detach_json = json_body(DETACH, "attachment state change")
purge_json = json_body(PURGE, "attachment purge")


def attachment_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/repairs/{repair_id}/attachments")

    @router.get("", response_model=AttachmentPage)
    def index(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
              before: str | None = None, limit: str | None = None):
        page, _ = attachments.list_attachments(workspace, access, repair_id,
                                               before=page_number(before), limit=page_number(limit, 25))
        return page

    @router.post("", response_model=Attachment, status_code=201, openapi_extra=UPLOAD_BODY)
    async def upload(repair_id: int, request: Request,
                     access: Annotated[RepairAccess, Depends(access_dependency)],
                     key: Annotated[str, Depends(idempotency_key)],
                     filename: str, entry_id: int | None = None, role: Literal["", "device"] = ""):
        declared = attachments.check_declared_length(request.headers.get("content-length"))
        staged = await run_in_threadpool(attachments.prepare_upload, workspace, access,
                                         repair_id, filename, entry_id, declared_size=declared)
        try:
            attachments.check_declared_length(request.headers.get("content-length"), staged.limit)
            await stream_body(request, staged)
        except BaseException:
            staged.discard()
            raise
        return await run_in_threadpool(lambda: attachments.commit_upload(
            workspace, access, repair_id, staged, filename=filename, entry_id=entry_id, key=key, role=role))

    @router.get("/{attachment_id}", response_model=Attachment)
    def detail(repair_id: int, attachment_id: int,
               access: Annotated[RepairAccess, Depends(access_dependency)]):
        return attachments.read_attachment(workspace, access, repair_id, attachment_id)

    @router.patch("/{attachment_id}", response_model=Attachment, openapi_extra=body_schema(CAPTION))
    def caption(repair_id: int, attachment_id: int,
                access: Annotated[RepairAccess, Depends(access_dependency)],
                change: Annotated[AttachmentCaption, Depends(caption_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return attachments.edit_caption(workspace, access, repair_id, attachment_id, change, key=key)

    @router.post("/{attachment_id}/links", response_model=Attachment, openapi_extra=body_schema(LINK))
    def links(repair_id: int, attachment_id: int,
              access: Annotated[RepairAccess, Depends(access_dependency)],
              change: Annotated[AttachmentLink, Depends(link_json)],
              key: Annotated[str, Depends(idempotency_key)]):
        return attachments.change_entry_link(workspace, access, repair_id, attachment_id, change, key=key)

    @router.get("/{attachment_id}/content",
                responses={200: {"content": {"application/octet-stream": {}}}})
    def content(repair_id: int, attachment_id: int,
                access: Annotated[RepairAccess, Depends(access_dependency)], inline: bool = False,
                if_none_match: Annotated[str | None, Header()] = None):
        found, source = attachments.attachment_content(workspace, access, repair_id, attachment_id,
                                                        inline=inline)
        return file_response(found, source, inline=inline, if_none_match=if_none_match)

    @router.post("/{attachment_id}/detach", response_model=Attachment, openapi_extra=body_schema(DETACH))
    def detach(repair_id: int, attachment_id: int,
               access: Annotated[RepairAccess, Depends(access_dependency)],
               change: Annotated[AttachmentDetach, Depends(detach_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return attachments.change_detached(workspace, access, repair_id, attachment_id, change, key=key)

    @router.post("/{attachment_id}/purge", response_model=Attachment, openapi_extra=body_schema(PURGE))
    def purge(repair_id: int, attachment_id: int,
              access: Annotated[RepairAccess, Depends(access_dependency)],
              request: Annotated[AttachmentPurge, Depends(purge_json)],
              key: Annotated[str, Depends(idempotency_key)]):
        return attachments.purge_attachment(workspace, access, repair_id, attachment_id, request, key=key)

    return router
