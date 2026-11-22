"""Inventory catalog through native browser forms with decimal EUR and quantity input."""

from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from repairlib import money
from repairlib.adapters.web.errors import refusal
from repairlib.adapters.web.security import (
    browser_access as access,
    check_form, decode_form, form_int, form_token, require_runtime,
)
from repairlib.contracts import ItemInput
from repairlib.domain import inventory, stock
from repairlib.domain.customers import MAX_ID, CustomerError

TEXT_FIELDS = tuple(inventory.FIELD_LIMITS)
NUMBER_FIELDS = (("low_stock", money.parse_threshold, "low_stock_milli"),
                 ("purchase_cost", money.parse_cents, "purchase_cost_cents"),
                 ("selling_price", money.parse_cents, "selling_price_cents"))
WEB_NAMES = {target: name for name, _, target in NUMBER_FIELDS}


async def item_form(request: Request):
    return await decode_form(request, max_fields=20, max_bytes=32 * 1024)


revision_of = form_int


def parsed(fields: dict[str, str]) -> ItemInput:
    errors = {}
    values = {}
    for name, parser, target in NUMBER_FIELDS:
        raw = fields.get(name, "")
        if not raw.strip() and name != "selling_price":
            values[target] = None if name == "purchase_cost" else 0
            continue
        try:
            values[target] = parser(raw)
        except ValueError as error:
            errors[name] = str(error)
    if errors:
        raise CustomerError(422, "validation", "Check the inventory item fields.", errors)
    return ItemInput(**{name: fields.get(name, "") for name in TEXT_FIELDS}, **values)


def form_values(item) -> dict[str, str]:
    return {**{name: getattr(item, name) for name in TEXT_FIELDS},
            "low_stock": money.format_milli(item.low_stock_milli),
            "purchase_cost": "" if item.purchase_cost_cents is None else money.format_cents(item.purchase_cost_cents),
            "selling_price": money.format_cents(item.selling_price_cents)}


def inventory_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/inventory", dependencies=[Depends(require_runtime)])

    def render(request, template="inventory.html", *, status=200, **context):
        return templates.TemplateResponse(request, template, context, status_code=status)

    def draft(request, *, fields=None, request_key=None, error=None, item_id=None, revision=None):
        purpose = f"inventory.edit:{item_id}" if item_id is not None else "inventory.create"
        field_errors = {WEB_NAMES.get(name, name): text for name, text in ((error.fields or {}) if error else {}).items()}
        return render(
            request, status=error.status if error else 200, form=True,
            details=fields or {"unit": "pcs", "low_stock": "0", "selling_price": "0.00"},
            limits=inventory.FIELD_LIMITS, csrf=form_token(request, key, purpose),
            request_key=request_key or uuid4().hex, error=str(error) if error else "",
            field_errors=field_errors, item_id=item_id, revision=revision,
            stale=error is not None and error.code == "stale_revision",
        )

    def failure(request, error):
        return refusal(request, error, lambda r, e: render(r, status=e.status, error=str(e)))

    def submit(request, form, purpose, item_id=None):
        actor = access(request)
        check_form(request, form, key, purpose)
        fields = {name: form.get(name, "") for name in (*TEXT_FIELDS, *WEB_NAMES.values())}
        request_key = form.get("key", "")
        revision = form.get("revision", "")
        try:
            details = parsed(fields)
            if item_id is None:
                item = inventory.create_item(workspace, actor, details, key=request_key)
            else:
                item = inventory.edit_item(workspace, actor, item_id, details,
                                           revision=revision_of(revision), key=request_key)
        except CustomerError as error:
            if error.status in (422, 409):
                return draft(request, fields=fields, request_key=request_key, error=error,
                             item_id=item_id, revision=revision)
            return failure(request, error)
        return RedirectResponse(request.url_for("inventory_detail", item_id=item.id).path, status_code=303)

    @router.get("")
    def inventory_list(request: Request, before: int | None = None, include_archived: bool = False,
                       low_stock: bool = False, q: str = "", category: str = ""):
        actor = access(request)
        try:
            page = inventory.list_items(workspace, actor, before=before, include_archived=include_archived,
                                        low_stock=low_stock, query=q, category=category)
            can_write = inventory.may_write(workspace, actor)
            categories = inventory.categories(workspace, actor)
        except CustomerError as error:
            return failure(request, error)
        return render(request, page=page, can_write=can_write, before=before,
                      include_archived=include_archived, low_stock=low_stock,
                      query=q, category=category, categories=categories)

    @router.get("/new")
    def inventory_new(request: Request):
        try:
            if not inventory.may_write(workspace, access(request)):
                return render(request, status=403, error="Your account cannot change inventory.")
        except CustomerError as error:
            return failure(request, error)
        return draft(request)

    @router.post("/new")
    def inventory_create(request: Request, form: Annotated[dict[str, str], Depends(item_form)]):
        return submit(request, form, "inventory.create")

    def detail_page(request, item_id, *, before=None, error=None, action=None, values=None, movement_id=None):
        actor = access(request)
        try:
            item = inventory.read_item(workspace, actor, item_id)
            movements = stock.list_movements(workspace, actor, item_id, before=before)
            can_write = inventory.may_write(workspace, actor)
            target = stock.read_movement(workspace, actor, item_id, movement_id) if movement_id else None
        except CustomerError as failed:
            return failure(request, failed)
        stale = error is not None and error.code == "stale_revision"
        reverse_retry = (target is not None and can_write and item.active and not target.reversed_by_id
                         and target.kind in ("receive", "adjust") and (stale or error.status == 422))
        return render(request, "inventory_detail.html", status=error.status if error else 200,
                      item=item, movements=movements, can_write=can_write, before=before,
                      reverse_target=target, reverse_retry=reverse_retry,
                      reverse_key=uuid4().hex if stale else (values or {}).get("key"),
                      keys={name: uuid4().hex for name in ("receive", "adjust", "reverse", "archive", "restore")},
                      csrf=form_token(request, key, f"inventory.item:{item_id}"),
                      error=str(error) if error else "", stale=stale,
                      field_errors={("quantity" if name == "quantity_milli" else name): text
                                    for name, text in ((error.fields or {}) if error else {}).items()},
                      stock_action=action, stock_values=values or {})

    @router.get("/{item_id}")
    def inventory_detail(request: Request, item_id: int, before: int | None = None):
        return detail_page(request, item_id, before=before)

    def stock_submit(request, form, item_id, action, movement_id=None):
        actor = access(request)
        check_form(request, form, key, f"inventory.item:{item_id}")
        values = {name: form.get(name, "") for name in ("key", "revision", "quantity", "reason", "direction")}
        common = {"revision": revision_of(values["revision"]), "key": values["key"]}
        before = revision_of(form.get("before", "")) if action == "reverse" else None
        before = before if before and before <= MAX_ID else None
        try:
            if action == "reverse":
                stock.reverse_movement(workspace, actor, item_id, movement_id, values["reason"], **common)
            else:
                try:
                    quantity = money.parse_milli(values["quantity"])
                except ValueError as error:
                    raise CustomerError(422, "validation", "Check the stock quantity.",
                                        {"quantity": str(error)}) from None
                if action == "receive":
                    stock.receive_stock(workspace, actor, item_id, quantity, reason=values["reason"], **common)
                else:
                    sign = 1 if values["direction"] == "add" else -1
                    stock.adjust_stock(workspace, actor, item_id, sign * quantity, values["reason"], **common)
        except CustomerError as error:
            if error.status in (422, 409):
                return detail_page(request, item_id, before=before, error=error, action=action, values=values,
                                   movement_id=movement_id)
            return failure(request, error)
        location = request.url_for("inventory_detail", item_id=item_id).path
        if action == "reverse" and before:
            location += "?" + urlencode({"before": before}) 
        return RedirectResponse(location, status_code=303)

    @router.post("/{item_id}/receive")
    def inventory_receive(request: Request, item_id: int, form: Annotated[dict[str, str], Depends(item_form)]):
        return stock_submit(request, form, item_id, "receive")

    @router.post("/{item_id}/adjust")
    def inventory_adjust(request: Request, item_id: int, form: Annotated[dict[str, str], Depends(item_form)]):
        return stock_submit(request, form, item_id, "adjust")

    @router.post("/{item_id}/movements/{movement_id}/reverse")
    def inventory_reverse(request: Request, item_id: int, movement_id: int,
                          form: Annotated[dict[str, str], Depends(item_form)]):
        return stock_submit(request, form, item_id, "reverse", movement_id)

    @router.get("/{item_id}/edit")
    def inventory_edit(request: Request, item_id: int):
        try:
            if not inventory.may_write(workspace, access(request)):
                return render(request, status=403, error="Your account cannot change inventory.")
            item = inventory.read_item(workspace, access(request), item_id)
        except CustomerError as error:
            return failure(request, error)
        if item.archived_at:
            return render(request, status=409, error="The inventory item is archived.", review_id=item_id)
        return draft(request, fields=form_values(item), item_id=item_id, revision=item.revision)

    @router.post("/{item_id}/edit")
    def inventory_save(request: Request, item_id: int, form: Annotated[dict[str, str], Depends(item_form)]):
        return submit(request, form, f"inventory.edit:{item_id}", item_id)

    def archive_submit(request, form, item_id, operation):
        actor = access(request)
        purpose = f"inventory.item:{item_id}"
        check_form(request, form, key, purpose)
        try:
            operation(workspace, actor, item_id, revision=revision_of(form.get("revision", "")),
                      key=form.get("key", ""))
        except CustomerError as error:
            if error.status in (422, 409):
                return render(request, status=error.status, error=str(error), review_id=item_id)
            return failure(request, error)
        return RedirectResponse(request.url_for("inventory_detail", item_id=item_id).path, status_code=303)

    @router.post("/{item_id}/archive")
    def inventory_archive(request: Request, item_id: int, form: Annotated[dict[str, str], Depends(item_form)]):
        return archive_submit(request, form, item_id, inventory.archive_item)

    @router.post("/{item_id}/restore")
    def inventory_restore(request: Request, item_id: int, form: Annotated[dict[str, str], Depends(item_form)]):
        return archive_submit(request, form, item_id, inventory.restore_item)

    return router
