"""Short-lived browser credentials, form bounds and password-work limits."""

from collections import deque
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
from hmac import compare_digest, digest
from math import ceil
import secrets
from threading import Lock
from time import monotonic
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request, Response

from repairlib.adapters.body import bounded_body
from repairlib.adapters.web.errors import typed_fields
from repairlib.domain.customers import CustomerAccess

BOOTSTRAP_SECONDS = 15 * 60
LOGIN_CSRF_SECONDS = 10 * 60
MAX_FORM_BYTES = 16 * 1024
SESSION_COOKIE = "repairlib_session"


def form_int(raw: str, digits: int = 19, *, default: int = 0) -> int:
    """A plain ASCII decimal of at most `digits` digits, or the caller's sentinel."""
    return int(raw) if len(raw) <= digits and raw.isascii() and raw.isdecimal() else default


def browser_access(request: Request) -> CustomerAccess:
    return CustomerAccess("web", request.cookies.get(SESSION_COOKIE, ""),
                          partial(setattr, request.state, "verified_actor"))


def require_runtime(request: Request) -> None:
    if request.app.state.workspace_status != "initialized":
        raise HTTPException(503, "The workspace is unavailable for sign-in.")


def matches(value: str, expected: str | None) -> bool:
    return expected is not None and compare_digest(value.encode(), expected.encode())


class Bootstrap:
    def __init__(self, token: str):
        self._link_hash = sha256(token.encode()).digest()
        self._grant_hash = None
        self._expires = monotonic() + BOOTSTRAP_SECONDS
        self._lock = Lock()

    def exchange(self, token: str) -> str | None:
        with self._lock:
            if self._link_hash is None or monotonic() >= self._expires:
                return None
            if not compare_digest(sha256(token.encode()).digest(), self._link_hash):
                return None
            grant = secrets.token_urlsafe(32)
            self._grant_hash = sha256(grant.encode()).digest()
            self._link_hash = None
            return grant

    def allows(self, grant: str) -> bool:
        with self._lock:
            return (
                self._grant_hash is not None and monotonic() < self._expires
                and compare_digest(sha256(grant.encode()).digest(), self._grant_hash)
            )

    def close(self) -> None:
        with self._lock:
            self._link_hash = self._grant_hash = None


class PasswordBudget:
    """Bound Argon2 CPU and memory use across the loopback listener."""

    def __init__(self):
        self._starts = deque()
        self._active = 0
        self._lock = Lock()

    @contextmanager
    def attempt(self):
        with self._lock:
            now = monotonic()
            while self._starts and now - self._starts[0] >= 60:
                self._starts.popleft()
            if len(self._starts) >= 5:
                wait = max(1, ceil(60 - (now - self._starts[0])))
            else:
                wait = 1 if self._active >= 2 else 0
            if not wait:
                self._starts.append(now)
                self._active += 1
        try:
            yield wait
        finally:
            if not wait:
                with self._lock:
                    self._active -= 1


def csrf_token(key: bytearray, purpose: str, credential: str) -> str:
    return digest(key, f"{purpose}:{credential}".encode(), "sha256").hex()


class FormExpired(HTTPException):
    """A stale or missing form token; carries the typed text so the error page can return it."""

    def __init__(self, fields: dict[str, str]):
        super().__init__(403, "Form expired. Open the page again and retry.")
        self.typed = typed_fields(fields)


def form_token(request: Request, key: bytearray, purpose: str) -> str:
    """The CSRF token a form rendered for the signed-in session and this purpose must carry."""
    return csrf_token(key, purpose, browser_access(request).credential)


def check_form(request: Request, fields: dict[str, str], key: bytearray, purpose: str) -> None:
    """Refuse a foreign origin, or a token that is missing or belongs to another session or purpose."""
    credential = browser_access(request).credential
    check_csrf(request, fields, csrf_token(key, purpose, credential) if credential else None)


def check_csrf(request: Request, fields: dict[str, str], expected: str | None) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Form origin is invalid.")
    if not matches(fields.get("csrf", ""), expected):
        raise FormExpired(fields)


def set_cookie(request: Request, response: Response, name: str, value: str, *,
               path: str, max_age: int | None) -> None:
    response.set_cookie(
        name, value, path=path, max_age=max_age,
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )


async def read_form(request: Request) -> dict[str, str]:
    return await decode_form(request, max_fields=5, max_bytes=MAX_FORM_BYTES)


async def read_token_form(request: Request) -> dict[str, str]:
    return await decode_form(request, max_fields=7, max_bytes=MAX_FORM_BYTES)


async def decode_form(request: Request, *, max_fields: int, max_bytes: int) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    body = await bounded_body(request, max_bytes)
    if content_type != "application/x-www-form-urlencoded":
        if content_type == "" and not body:
            return {}
        raise HTTPException(415, "Unsupported form encoding.")
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=max_fields)
    except ValueError:
        raise HTTPException(400, "Invalid form data.") from None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise HTTPException(400, "Duplicate form fields.")
    return fields
