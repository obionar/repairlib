"""Customer records and history through native browser forms."""

from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from repairlib.adapters.web.errors import refusal
from repairlib.adapters.web.security import (
    browser_access as access,
    check_form, decode_form, form_int, form_token, require_runtime,
)
from repairlib.contracts import CustomerArchiveChange, CustomerInput, CustomerPatch, CustomerWithNotices
from repairlib.domain import customers, repairs
from repairlib.domain.customers import CustomerError


async def customer_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=32 * 1024)


def customer_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/customers", dependencies=[Depends(require_runtime)])

    def render(request, template="customers.html", *, status=200, **context):
        return templates.TemplateResponse(request, template, context, status_code=status)

    def draft(request, *, fields=None, request_key=None, error=None, customer_id=None, revision=None,
              next_repair=False):
        purpose = f"customer.update:{customer_id}" if customer_id is not None else "customer.create"
        return render(
            request, status=error.status if error else 200, form=True,
            details=fields or {}, limits=customers.FIELD_LIMITS,
            csrf=form_token(request, key, purpose),
            request_key=request_key or uuid4().hex, error=str(error) if error else "",
            field_errors=(error.fields or {}) if error else {},
            contact_warnings=customers.contact_warnings(CustomerInput(**fields)) if fields else {},
            customer_id=customer_id, revision=revision, next_repair=next_repair,
            stale=error is not None and error.code == "stale_revision",
        )

    def failure(request, error):
        return refusal(request, error, lambda r, e: render(r, status=e.status, error=str(e)))

    @router.get("")
    def customer_list(request: Request, before: int | None = None, q: str = "",
                      include_archived: bool = False):
        try:
            page, can_write = customers.list_customers(
                workspace, access(request), before=before, query=q, include_archived=include_archived,
            )
        except CustomerError as error:
            return failure(request, error)
        return render(request, page=page, can_write=can_write, before=before, query=q,
                      include_archived=include_archived)

    @router.get("/new")
    def customer_new(request: Request, next: str = ""):
        try:
            if not customers.may_write(workspace, access(request)):
                return render(request, status=403, error="Your account cannot create customers.")
        except CustomerError as error:
            return failure(request, error)
        return draft(request, next_repair=next == "new-repair")

    @router.post("/new")
    def customer_create(request: Request, form: Annotated[dict[str, str], Depends(customer_form)]):
        check_form(request, form, key, "customer.create")
        fields = {name: form.get(name, "") for name in customers.FIELD_LIMITS}
        request_key = form.get("key", "")
        next_repair = form.get("next") == "new-repair"
        try:
            created = customers.create_customer(workspace, access(request), CustomerInput(**fields), key=request_key)
        except CustomerError as error:
            if error.status in (422, 409):
                return draft(request, fields=fields, request_key=request_key, error=error, next_repair=next_repair)
            return failure(request, error)
        query = {}
        if next_repair:
            target = request.url_for("repair_new").path
            query["customer_id"] = created.id
        elif isinstance(created, CustomerWithNotices):
            target = request.url_for("customer_detail", customer_id=created.id).path
        else:
            target = request.url_for("customer_list").path
        if isinstance(created, CustomerWithNotices):
            query["customer_receipt"] = request_key
        if query:
            target += "?" + urlencode(query)
        return RedirectResponse(target, status_code=303)

    @router.get("/{customer_id}")
    def customer_detail(request: Request, customer_id: int, before: int | None = None,
                        repairs_before: int | None = None, customer_receipt: str = ""):
        actor = access(request)
        try:
            record, history, can_write = customers.read_customer_history(
                workspace, actor, customer_id, before=before,
            )
            linked, _ = repairs.list_repairs(workspace, actor, customer_id=customer_id,
                                            before=repairs_before)
            notice = customers.read_create_notice(workspace, actor, customer_id, customer_receipt)
        except CustomerError as error:
            return failure(request, error)

        def page_url(anchor, **cursors):
            query = urlencode({name: value for name, value in cursors.items() if value})
            return ("?" + query if query else request.url.path) + "#" + anchor

        return render(request, "customer_detail.html", record=record, history=history, linked=linked,
                      contact_warnings=customers.contact_warnings(record),
                      possible_duplicate_id=notice.possible_duplicate_id if notice else None,
                      older_history=page_url("customer-history", before=history.next_before,
                                             repairs_before=repairs_before) if history.next_before else "",
                      newest_history=page_url("customer-history", repairs_before=repairs_before) if before else "",
                      older_repairs=page_url("customer-repairs", before=before,
                                             repairs_before=linked.next_before) if linked.next_before else "",
                      newest_repairs=page_url("customer-repairs", before=before) if repairs_before else "",
                      can_write=can_write, request_key=uuid4().hex,
                      csrf=form_token(request, key, f"customer.archive:{customer_id}"))

    @router.post("/{customer_id}/archive")
    def customer_archive(request: Request, customer_id: int,
                         form: Annotated[dict[str, str], Depends(customer_form)]):
        actor = access(request)
        purpose = f"customer.archive:{customer_id}"
        check_form(request, form, key, purpose)
        enabled = form.get("enabled")
        revision = form_int(form.get("revision", ""))
        try:
            if enabled not in {"0", "1"}:
                raise CustomerError(422, "validation", "Choose whether to archive or restore the customer.")
            customers.change_customer_archive(workspace, actor, customer_id,
                                              CustomerArchiveChange(revision, enabled == "1"),
                                              key=form.get("key", ""))
        except CustomerError as error:
            if error.status in (422, 409):
                return render(request, status=error.status, error=str(error), review_id=customer_id)
            return failure(request, error)
        return RedirectResponse(request.url_for("customer_detail", customer_id=customer_id).path, status_code=303)

    @router.get("/{customer_id}/edit")
    def customer_edit(request: Request, customer_id: int):
        try:
            record, can_write = customers.read_customer(workspace, access(request), customer_id)
        except CustomerError as error:
            return failure(request, error)
        if not can_write:
            return render(request, status=403, error="Your account cannot edit customers.")
        fields = {name: getattr(record, name) for name in customers.FIELD_LIMITS}
        return draft(request, fields=fields, customer_id=customer_id, revision=record.revision)

    @router.post("/{customer_id}/edit")
    def customer_save(request: Request, customer_id: int,
                      form: Annotated[dict[str, str], Depends(customer_form)]):
        purpose = f"customer.update:{customer_id}"
        check_form(request, form, key, purpose)
        values = {name: form.get(name, "") for name in customers.FIELD_LIMITS}
        revision = form.get("revision", "")
        reviewed = form_int(revision)
        request_key = form.get("key", "")
        try:
            updated = customers.update_customer(workspace, access(request), customer_id,
                                                CustomerPatch(revision=reviewed, **values), key=request_key)
        except CustomerError as error:
            if error.status in (422, 409):
                return draft(request, fields=values, request_key=request_key, error=error,
                             customer_id=customer_id, revision=revision)
            return failure(request, error)
        target = (request.url_for("customer_detail", customer_id=updated.id)
                  if isinstance(updated, CustomerWithNotices) else request.url_for("customer_list"))
        return RedirectResponse(target.path, status_code=303)

    return router
