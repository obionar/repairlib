"""Account pages for creating and revoking API tokens."""

from pathlib import Path
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from repairlib.adapters.web.security import (
    PasswordBudget, browser_access, check_csrf, csrf_token, form_int, read_token_form, require_runtime,
)
from repairlib.domain import tokens

Form = Annotated[dict[str, str], Depends(read_token_form)]


def token_routes(workspace: Path, templates: Jinja2Templates, key: bytearray,
                 budget: PasswordBudget) -> APIRouter:
    router = APIRouter(prefix="/account/tokens", dependencies=[Depends(require_runtime)])

    def render(request, *, status=200, headers=None, **context):
        return templates.TemplateResponse(request, "tokens.html", context, status_code=status,
                                          headers=headers)

    def listing(request, *, before=0, fields=None, **context):
        access = browser_access(request)
        session = access.credential
        try:
            owner, items, next_page = tokens.read_tokens(workspace, session, now=int(time()),
                                                         before=before, on_verified=access.on_verified)
        except PermissionError:
            return RedirectResponse(request.url_for("sign_in").path, status_code=303)
        except ValueError:
            return render(request, status=422, error="Invalid token page.")
        return render(
            request, listing=True, items=items, next_page=next_page, before=before,
            fields=fields or {"key": uuid4().hex, "scope": "read", "kind": "user", "preset": ""},
            csrf=csrf_token(key, "token.create", session), scopes=tokens.token_scopes(owner.role),
            **context,
        )

    @router.get("")
    def api_tokens(request: Request, before: Annotated[int, Query(ge=0)] = 0):
        return listing(request, before=before)

    @router.post("")
    def issue_token(request: Request, form: Form):
        access = browser_access(request)
        session = access.credential
        check_csrf(request, form, csrf_token(key, "token.create", session) if session else None)
        fields = {field: form.get(field, "") for field in ("name", "scope", "key")}
        kind = form.get("kind", "user")
        preset = form.get("preset", "") or None
        shown = {**fields, "kind": kind, "preset": preset or ""}
        with budget.attempt(request) as wait:
            if wait:
                return listing(request, fields=shown, status=429, headers={"Retry-After": str(wait)},
                               error="Too many attempts. Wait a moment and try again.")
            try:
                secret = tokens.create_token(workspace, session, **fields, kind=kind, preset=preset,
                                             password=form.get("password", ""), now=int(time()),
                                             on_verified=access.on_verified)
            except (PermissionError, ValueError, tokens.TokenConflict) as exc:
                status = mutation_status(exc)
                if status == 403:
                    return render(request, status=status, error=str(exc))
                return listing(request, fields=shown, status=status, error=str(exc))
        if secret is None:
            return render(request, message="This request already created a token. The secret is shown once.")
        return render(request, status=201, secret=secret)

    @router.get("/{token_id}/revoke")
    def review_revocation(request: Request, token_id: int):
        access = browser_access(request)
        session = access.credential
        try:
            token = tokens.read_token(workspace, session, now=int(time()), token_id=token_id,
                                      on_verified=access.on_verified)
        except PermissionError:
            return RedirectResponse(request.url_for("sign_in").path, status_code=303)
        except tokens.TokenMissing as exc:
            return render(request, status=404, error=str(exc))
        return render(request, revoke_id=token_id, token_name=token.name,
                      csrf=csrf_token(key, f"token.revoke:{token_id}", session),
                      revision=token.revision, request_key=uuid4().hex)

    @router.post("/{token_id}/revoke")
    def submit_revocation(request: Request, token_id: int, form: Form):
        access = browser_access(request)
        session = access.credential
        csrf = csrf_token(key, f"token.revoke:{token_id}", session) if session else None
        check_csrf(request, form, csrf)
        revision = form_int(form.get("revision", ""))
        request_key = form.get("key", "")
        with budget.attempt(request) as wait:
            if wait:
                try:
                    token = tokens.read_token(workspace, session, now=int(time()), token_id=token_id,
                                              on_verified=access.on_verified)
                except (PermissionError, tokens.TokenMissing):
                    return render(request, status=429, headers={"Retry-After": str(wait)},
                                  error="Too many attempts. Wait a moment and try again.")
                return render(request, status=429, headers={"Retry-After": str(wait)},
                              error="Too many attempts. Wait a moment and try again.",
                              revoke_id=token_id, token_name=token.name, csrf=csrf,
                              revision=revision, request_key=request_key)
            try:
                tokens.revoke_token(workspace, session, token_id=token_id, revision=revision,
                                    password=form.get("password", ""), key=request_key, now=int(time()),
                                    on_verified=access.on_verified)
            except (PermissionError, ValueError, tokens.TokenConflict, tokens.TokenMissing) as exc:
                return render(request, status=mutation_status(exc), error=str(exc))
        return RedirectResponse(request.url_for("api_tokens").path, status_code=303)

    return router


def mutation_status(error: Exception) -> int:
    if isinstance(error, PermissionError):
        return 403
    if isinstance(error, tokens.TokenMissing):
        return 404
    if isinstance(error, tokens.TokenConflict):
        return 409
    return 422
