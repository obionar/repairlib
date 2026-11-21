"""FastAPI factory with read-only startup and process health."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
import html
from http import HTTPStatus
import logging
from pathlib import Path
import re
from secrets import token_bytes
import traceback
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from repairlib.adapters.api.routes import customer_error, error, mount_api, validation_error
from repairlib import i18n
from repairlib.adapters.web.errors import DEFAULT_HEADING, PAGES, error_page, is_api
from repairlib.adapters.web.routes import ASSETS, browser_routes
from repairlib.adapters.web.security import Bootstrap
from repairlib.config import LOOPBACK_BIND_HOSTS, resolve_workspace
from repairlib.domain.customers import CustomerError
from repairlib.lifecycle.workspace import WorkspaceError, load_form_key, maintenance_lock, workspace_status
from repairlib.storage import StorageOperationalError

logger = logging.getLogger("uvicorn.error.repairlib")


# Subresource hash of static/live-search.js; templates carry the same value.
LIVE_SEARCH_SHA = "thF48QvDEvENP4+0Vm17obSkSDvRs0HYr8eH7A/U3HI="


def create_app(
    *, workspace: str | Path | None = None, bootstrap_token: str | None = None,
    require_initialized: bool = False,
    listener_state: Callable[[], dict] | None = None, listener_id: str | None = None,
) -> FastAPI:
    resolved = resolve_workspace(workspace)
    bootstrap = Bootstrap(bootstrap_token) if bootstrap_token else None
    form_key = bytearray(token_bytes(32))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        status = workspace_status(resolved)
        with ExitStack() as locks:
            if status == "initialized":
                try:
                    locks.enter_context(maintenance_lock(resolved, exclusive=False))
                except WorkspaceError:
                    status = "maintenance"
                except OSError:
                    status = "invalid"
                else:
                    status = workspace_status(resolved)
            if require_initialized and status != "initialized":
                raise WorkspaceError(f"Service workspace startup refused ({status}).")
            if status == "initialized":
                form_key[:] = load_form_key(resolved)
            app.state.workspace_status = status
            yield

    app = FastAPI(title="RepairLib", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/v1/openapi.json")
    allowed_hosts = [re.escape(f"[{host}]" if ":" in host else host) for host in LOOPBACK_BIND_HOSTS]
    # Older Starlette splits Host at its first colon, including inside IPv6 brackets.
    loopback_authority = re.compile(rf"(?:{'|'.join(allowed_hosts)})(?::[0-9]{{1,5}})?")

    async def browser_error(request: Request, status: int, message: str, typed=(), request_id=None):
        """Translated error page; a bare page in the same language if the full one cannot render."""
        try:
            return await run_in_threadpool(error_page, request, status, message, typed, request_id)
        except Exception:  # the shell lookup itself may be what failed
            language = i18n.negotiate(request.cookies.get(i18n.COOKIE, ""),
                                      request.headers.get("accept-language", ""))
            say = i18n.translator(language)
            heading = html.escape(say(PAGES.get(status, DEFAULT_HEADING)))
            return HTMLResponse(
                f'<!doctype html><html lang="{language}"><meta charset="utf-8">'
                f'<title>{heading}</title><main><h1>{heading}</h1><p>{html.escape(say(message))}</p>'
                f'<p>{html.escape(say("Request ID"))}: {request_id or ""}</p>'
                f'<p><a href="/">{html.escape(say("Go back"))}</a></p></main></html>',
                status_code=status)

    async def http_error(request: Request, exc: StarletteHTTPException):
        if is_api(request) or not hasattr(request.app.state, "render_error"):
            return await http_exception_handler(request, exc)
        phrase = HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus._value2member_map_ else ""
        message = "" if exc.detail in (None, phrase) else str(exc.detail)
        response = await browser_error(request, exc.status_code, message, getattr(exc, "typed", ()))
        response.headers.update(exc.headers or {})
        return response

    class SecurityMiddleware:
        """Pure ASGI, so behaviour does not depend on the Starlette release's call_next."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.inner(scope, receive, send)
                return
            request = Request(scope, receive)
            started = False

            async def finish(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    headers = MutableHeaders(raw=message["headers"])
                    self.decorate(request, message["status"], headers)
                await send(message)

            try:
                hosts = request.headers.getlist("host")
                if len(hosts) != 1 or not loopback_authority.fullmatch(hosts[0]):
                    request.state.error_code = "invalid_host"
                    response = PlainTextResponse("Invalid host header", status_code=400)
                elif (listener_state is not None and not listener_state()["writes_open"]
                        and (request.method, request.url.path) != ("GET", "/health")):
                    response = JSONResponse({"detail": "Candidate writes are closed."}, status_code=503)
                else:
                    await self.inner(scope, receive, finish)
                    return
            except Exception as exc:
                if started:
                    raise
                response = await self.failure(request, exc)
            await response(scope, receive, finish)

        async def failure(self, request: Request, exc: Exception):
            busy = isinstance(exc, StorageOperationalError) and (
                getattr(exc, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))
                or str(exc) in {"database is locked", "database table is locked"}
            )
            status, code, message = (
                (503, "busy", "The workspace is busy. Try again shortly.") if busy else
                (500, "server_error", "An unexpected error occurred.")
            )
            request.state.error_code = code
            if not busy:
                # Keep stack locations and type, never exception messages or source text.
                request.state.failure_stack = (type(exc).__name__, [
                    (frame.filename, frame.lineno, frame.name)
                    for frame in traceback.extract_tb(exc.__traceback__)
                ])
            if is_api(request):
                return error(status, code, message)
            request_id = uuid4().hex
            response = await browser_error(request, status, message, request_id=request_id)
            response.headers["X-Request-ID"] = request_id
            return response

        @staticmethod
        def decorate(request: Request, status: int, headers: MutableHeaders) -> None:
            if status >= 400:
                request_id = headers.setdefault("X-Request-ID", uuid4().hex)
                # Use the route template, never a URL containing user data.
                route = getattr(request.scope.get("route"), "path", "unmatched")
                shop_id, actor_id = getattr(request.state, "verified_actor", (None, None))
                logger.log(logging.ERROR if status >= 500 else logging.INFO,
                           "request_id=%s operation=%r status=%s code=%s shop_id=%s actor_id=%s stack=%r",
                           request_id, route, status, getattr(request.state, "error_code", "refused"),
                           shop_id, actor_id, getattr(request.state, "failure_stack", None))
            # A no-referrer form page can turn Chromium's navigation Origin into null.
            referrer_policy = "no-referrer" if request.url.path == "/setup/exchange" else "same-origin"
            if "cache-control" not in headers:  # only attachment previews opt out
                headers["Cache-Control"] = "no-store"
            headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; connect-src 'self'; "
                f"script-src 'sha256-{LIVE_SEARCH_SHA}' 'sha256-qUn3bIvHbKqO38YIYPpoceYlhO2diYxt1R4uatgmOdA='; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
            headers["Referrer-Policy"] = referrer_policy
            headers["X-Content-Type-Options"] = "nosniff"

    app.add_middleware(SecurityMiddleware)

    app.include_router(browser_routes(resolved, bootstrap, form_key, app))
    mount_api(app, resolved)
    app.add_exception_handler(CustomerError, customer_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")

    @app.get("/health", include_in_schema=False)
    async def health(response: Response) -> dict[str, object]:
        status = app.state.workspace_status
        selection = listener_state() if listener_state is not None else {}
        unavailable = selection.get("update_phase") == "unavailable"
        if unavailable or status in {"maintenance", "invalid", "unsupported_schema", "upgrade_required"}:
            response.status_code = 503
        return {
            "status": "ok",
            "state": "uninitialized" if status == "missing" else status,
            "ready": status == "initialized" and not unavailable,
            "workspace_exists": status != "missing",
            **({"listener_id": listener_id, **selection} if listener_id is not None else {}),
        }

    return app
