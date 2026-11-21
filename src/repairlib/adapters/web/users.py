"""Native forms for same-shop user and role administration."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from repairlib.adapters.web.errors import refusal
from repairlib.adapters.web.security import (
    browser_access as access,
    check_form, decode_form, form_int, form_token, require_runtime,
)
from repairlib.contracts import UserInput, UserPatch
from repairlib.domain import users
from repairlib.domain.users import UserError


async def user_form(request: Request):
    return await decode_form(request, max_fields=7, max_bytes=16 * 1024)


def user_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/users", dependencies=[Depends(require_runtime)])

    def render(request, *, status=200, **context):
        return templates.TemplateResponse(request, "users.html", context, status_code=status)

    def failure(request, error):
        return refusal(request, error, lambda r, e: render(r, status=e.status, error=str(e)))

    def draft(request, *, fields=None, request_key=None, error=None, user_id=None, revision=None):
        purpose = f"user.update:{user_id}" if user_id is not None else "user.create"
        return render(
            request, status=error.status if error else 200, form=True,
            details=fields or {}, csrf=form_token(request, key, purpose),
            request_key=request_key or uuid4().hex, error=str(error) if error else "",
            field_errors=(error.fields or {}) if error else {}, roles=sorted(users.ROLES),
            user_id=user_id, revision=revision,
            stale=error is not None and error.code == "stale_revision",
        )

    @router.get("")
    def user_list(request: Request, before: int | None = None):
        try:
            page = users.list_users(workspace, access(request), before=before)
        except UserError as error:
            return failure(request, error)
        return render(request, page=page, before=before)

    @router.get("/new")
    def user_new(request: Request):
        try:
            users.list_users(workspace, access(request), limit=1)
        except UserError as error:
            return failure(request, error)
        return draft(request, fields={"role": "technician", "active": True})

    @router.post("/new")
    def user_create(request: Request, form: Annotated[dict[str, str], Depends(user_form)]):
        check_form(request, form, key, "user.create")
        fields = {name: form.get(name, "") for name in ("username", "role", "password")}
        request_key = form.get("key", "")
        try:
            users.create_user(workspace, access(request), UserInput(**fields), key=request_key)
        except UserError as error:
            if error.status in (422, 409):
                return draft(request, fields={**fields, "password": ""},
                             request_key=request_key, error=error)
            return failure(request, error)
        return RedirectResponse(request.url_for("user_list").path, status_code=303)

    @router.get("/{user_id}/edit")
    def user_edit(request: Request, user_id: int):
        try:
            record = users.read_user(workspace, access(request), user_id)
        except UserError as error:
            return failure(request, error)
        fields = {"username": record.username, "role": record.role, "active": record.active}
        return draft(request, fields=fields, user_id=user_id, revision=record.revision)

    @router.post("/{user_id}/edit")
    def user_save(request: Request, user_id: int,
                  form: Annotated[dict[str, str], Depends(user_form)]):
        purpose = f"user.update:{user_id}"
        check_form(request, form, key, purpose)
        revision = form.get("revision", "")
        reviewed = form_int(revision)
        fields = {
            "username": form.get("username", ""), "role": form.get("role", ""),
            "active": form.get("active") == "1",
        }
        password = form.get("password", "")
        request_key = form.get("key", "")
        patch = UserPatch(revision=reviewed, **fields, password=password or None)
        try:
            users.update_user(workspace, access(request), user_id, patch, key=request_key)
        except UserError as error:
            if error.status in (422, 409):
                return draft(request, fields=fields, request_key=request_key, error=error,
                             user_id=user_id, revision=revision)
            return failure(request, error)
        return RedirectResponse(request.url_for("user_list").path, status_code=303)

    return router
