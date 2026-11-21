"""Versioned bearer-authenticated HTTP operations."""

from functools import partial
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.attachments import attachment_routes
from repairlib.adapters.api.charges import charge_routes
from repairlib.adapters.api.customers import customer_routes
from repairlib.adapters.api.csv_intake import csv_routes
from repairlib.adapters.api.inventory import inventory_routes
from repairlib.adapters.api.portable_shop import portable_routes
from repairlib.adapters.api.repairs import repair_routes
from repairlib.adapters.api.settings import settings_routes
from repairlib.adapters.api.users import user_routes
from repairlib.domain.customers import CustomerAccess, CustomerError
from repairlib.domain.dashboard import read_dashboard
from repairlib.domain.tokens import authenticate_api_token
from repairlib.contracts import Dashboard, TokenIdentity

bearer = HTTPBearer(auto_error=False)


class APIError(BaseModel):
    code: str
    message: str
    request_id: str
    fields: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    error: APIError


def mount_api(app, workspace: Path) -> None:
    """Add the versioned API to `app`. Sub-routers go straight onto the app so FastAPI builds each route once
    per include level instead of re-processing a nested `/api/v1` router (about a third of startup time)."""
    router = APIRouter(prefix=API_PREFIX)

    def access(request: Request,
               auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if request.app.state.workspace_status != "initialized":
            raise CustomerError(503, "unavailable", "The workspace is unavailable.")
        if auth is None or len(request.headers.getlist("authorization")) != 1:
            raise CustomerError(401, "unauthenticated", "A valid API token is required.")
        return CustomerAccess("api", auth.credentials, partial(setattr, request.state, "verified_actor"))

    for routes in (customer_routes, csv_routes, repair_routes, attachment_routes, charge_routes,
                   inventory_routes, user_routes, settings_routes, portable_routes):
        app.include_router(routes(workspace, access))

    @router.get("/dashboard", response_model=Dashboard)
    def dashboard(principal_access: Annotated[CustomerAccess, Depends(access)]):
        return read_dashboard(workspace, principal_access)

    @router.get("/identity", response_model=TokenIdentity, response_model_exclude_defaults=True,
                responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
    def identity(request: Request,
                 auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if request.app.state.workspace_status != "initialized":
            return error(503, "unavailable", "The workspace is unavailable.")
        if auth is not None and len(request.headers.getlist("authorization")) == 1:
            principal = authenticate_api_token(workspace, auth.credentials)
            if principal is not None:
                request.state.verified_actor = principal.shop_id, principal.user_id
                return principal
        return error(401, "unauthenticated", "A valid API token is required.")

    app.include_router(router)


def error(status: int, code: str, message: str, fields=None) -> JSONResponse:
    body = ErrorResponse(error=APIError(code=code, message=message, request_id=uuid4().hex,
                                        fields=fields))
    headers = {"X-Request-ID": body.error.request_id}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


async def customer_error(request: Request, exc: CustomerError) -> JSONResponse:
    request.state.error_code = exc.code
    return error(exc.status, exc.code, str(exc), exc.fields)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    if path.startswith("/api/v1/"):
        return error(422, "validation", "Check the request path, parameters and headers.")
    return await request_validation_exception_handler(request, exc)
