"""Shared streaming upload and safe download helpers for HTTP adapters."""

from typing import BinaryIO
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from repairlib.contracts import Attachment
from repairlib.domain import attachments


async def stream_body(request, upload) -> None:
    """Copy the raw request body into staging off the event loop, enforcing the limit."""
    async for chunk in request.stream():
        if chunk:
            await run_in_threadpool(attachments.write_chunk, upload, chunk)


def disposition(kind: str, filename: str) -> str:
    fallback = "".join(char if char.isascii() and char.isprintable() and char not in '"\\;' else "_"
                       for char in filename)
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


PREVIEW_CACHE = "private, no-cache, must-revalidate, no-transform"


def matches_etag(header: str | None, etag: str) -> bool:
    candidates = [part.strip().removeprefix("W/") for part in (header or "").split(",")]
    return etag in candidates or "*" in candidates


class AttachmentResponse(StreamingResponse):
    def __init__(self, source: BinaryIO, size: int, **kwargs):
        self.source = source
        self.size = size
        super().__init__(self.chunks(), **kwargs)

    def chunks(self):
        remaining = self.size
        while remaining > 0 and (chunk := self.source.read(min(1 << 20, remaining))):
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.source.close()


def file_response(attachment: Attachment, source: BinaryIO, *, inline: bool, if_none_match: str | None = None):
    headers = {"Cross-Origin-Resource-Policy": "same-origin"}
    try:
        if inline:
            headers.update(ETag=f'"{attachment.sha256}"', **{"Cache-Control": PREVIEW_CACHE})
            if matches_etag(if_none_match, headers["ETag"]):
                source.close()
                return Response(status_code=304, headers=headers)
        media_type = attachment.media_type if inline else "application/octet-stream"
        headers["Content-Disposition"] = disposition("inline" if inline else "attachment", attachment.filename)
        headers["Content-Length"] = str(attachment.size_bytes)
        return AttachmentResponse(source, attachment.size_bytes, media_type=media_type, headers=headers)
    except BaseException:
        source.close()
        raise
