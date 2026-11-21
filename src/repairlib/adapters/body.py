"""Bound untrusted request bodies before decoding."""

from fastapi import HTTPException, Request

API_PREFIX = "/api/v1"


async def bounded_body(request: Request, limit: int) -> bytearray:
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, "Invalid request length.")
        digits = length.lstrip("0")
        if len(digits) > len(str(limit)) or int(digits or "0") > limit:
            raise HTTPException(413, "Request is too large.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Request is too large.")
        body.extend(chunk)
    return body
