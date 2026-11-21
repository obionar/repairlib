"""Streaming multipart parser for no-JavaScript browser uploads."""

from fastapi import HTTPException
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool

from repairlib.domain import attachments

MAX_FIELDS = 4
MAX_FIELD_BYTES = 1024
MULTIPART_OVERHEAD = 64 * 1024


class _Receiver:
    def __init__(self, upload):
        self.upload = upload
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.parts = 0
        self._reset()

    def _reset(self):
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.headers: dict[bytes, bytes] = {}
        self.name = ""
        self.is_file = False
        self.buffer = bytearray()

    def on_part_begin(self):
        self._reset()
        self.parts += 1
        if self.parts > MAX_FIELDS + 1:
            raise HTTPException(400, "Too many form fields.")

    def on_header_field(self, data, start, end):
        self.header_field += data[start:end]

    def on_header_value(self, data, start, end):
        self.header_value += data[start:end]

    def on_header_end(self):
        self.headers[bytes(self.header_field).lower()] = bytes(self.header_value)
        self.header_field = bytearray()
        self.header_value = bytearray()

    def on_headers_finished(self):
        kind, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if kind != b"form-data" or b"name" not in options:
            raise HTTPException(400, "Invalid form data.")
        try:
            self.name = options[b"name"].decode("ascii")
            filename = options.get(b"filename")
            if filename is not None:
                if self.name != "file" or self.filename is not None:
                    raise HTTPException(400, "Send one file in the file field.")
                self.filename = filename.decode("utf-8")
                self.is_file = True
        except UnicodeDecodeError:
            raise HTTPException(400, "Invalid form data.") from None
        if not self.is_file and self.name in self.fields:
            raise HTTPException(400, "Duplicate form fields.")

    def on_part_data(self, data, start, end):
        if self.is_file:
            attachments.write_chunk(self.upload, bytes(data[start:end]))
        else:
            self.buffer += data[start:end]
            if len(self.buffer) > MAX_FIELD_BYTES:
                raise HTTPException(400, "Form field is too large.")

    def on_part_end(self):
        if not self.is_file:
            try:
                self.fields[self.name] = self.buffer.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(400, "Invalid form data.") from None


async def receive_upload(request, upload) -> tuple[dict[str, str], str | None]:
    """Stream one multipart file into `upload`; return small text fields and the filename."""
    kind, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise HTTPException(415, "Unsupported form encoding.")
    receiver = _Receiver(upload)
    callbacks = {name: getattr(receiver, name) for name in (
        "on_part_begin", "on_header_field", "on_header_value", "on_header_end",
        "on_headers_finished", "on_part_data", "on_part_end")}
    parser = MultipartParser(boundary, callbacks)
    limit = upload.limit + MULTIPART_OVERHEAD
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise attachments.too_large(upload.limit)
        if chunk:
            try:
                await run_in_threadpool(parser.write, chunk)
            except MultipartParseError:
                raise HTTPException(400, "Invalid form data.") from None
    try:
        parser.finalize()
    except MultipartParseError:
        raise HTTPException(400, "Invalid form data.") from None
    return receiver.fields, receiver.filename
