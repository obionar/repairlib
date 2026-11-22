"""Native forms for repair billing and stock returns with decimal input."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from repairlib import money
from repairlib.adapters.web.errors import refusal
from repairlib.adapters.web.security import (
    browser_access as access,
    check_form, decode_form, form_int, form_token, require_runtime,
)
from repairlib.contracts import ChargeInput, ChargePatch, ChargeVisibility, IntakeMistake, PartReturn
from repairlib.domain import charges, stock
from repairlib.domain.repairs import RepairError

TEXT_FIELDS = ("kind", "description", "quantity", "unit", "unit_price", "unit_cost", "notes")


async def charge_form(request: Request):
    return await decode_form(request, max_fields=12, max_bytes=16 * 1024)


revision_of = form_int


def parsed(fields: dict[str, str]) -> ChargeInput:
    errors = {}
    values = {}
    for name, parser, target in (("quantity", money.parse_milli, "quantity_milli"),
                                 ("unit_price", money.parse_cents, "unit_price_cents"),
                                 ("unit_cost", money.parse_cents, "unit_cost_cents")):
        raw = fields.get(name, "")
        if name == "unit_cost" and not raw.strip():
            values[target] = None
            continue
        try:
            values[target] = parser(raw)
        except ValueError as error:
            errors[name] = str(error)
    if errors:
        raise RepairError(422, "validation", "Check the charge fields.", errors)
    return ChargeInput(kind=fields.get("kind", ""), description=fields.get("description", ""),
                       unit=fields.get("unit", ""), notes=fields.get("notes", ""), **values)


def form_values(charge) -> dict[str, str]:
    return {"kind": charge.kind, "description": charge.description,
            "quantity": money.format_milli(charge.quantity_milli), "unit": charge.unit,
            "unit_price": money.format_cents(charge.unit_price_cents),
            "unit_cost": "" if charge.unit_cost_cents is None else money.format_cents(charge.unit_cost_cents),
            "notes": charge.notes}


def web_errors(error: RepairError) -> dict[str, str]:
    names = {"quantity_milli": "quantity", "unit_price_cents": "unit_price", "unit_cost_cents": "unit_cost"}
    return {names.get(name, name): message for name, message in (error.fields or {}).items()}


def charge_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/repairs/{repair_id}/charges", dependencies=[Depends(require_runtime)])

    def failure(request, error):
        return refusal(request, error, lambda r, e: templates.TemplateResponse(
            r, "repairs.html", {"error": str(e)}, status_code=e.status))

    def page(request, repair_id, *, charge=None, details=None, revision=None, request_key=None,
             error=None, status=200, visibility_key=None):
        purpose = f"charge.correct:{charge.id}" if charge else f"charge.create:{repair_id}"
        return templates.TemplateResponse(request, "charge_form.html", {
            "repair_id": repair_id, "charge": charge, "details": details or {"kind": "part"},
            "revision": revision, "request_key": request_key or uuid4().hex,
            "csrf": form_token(request, key, purpose),
            "visibility_csrf": form_token(request, key, f"charge.visibility:{charge.id}") if charge else "",
            "visibility_key": visibility_key or uuid4().hex,
            "error": str(error) if error else "", "field_errors": web_errors(error) if error else {},
        }, status_code=status)

    @router.get("/new")
    def charge_new(request: Request, repair_id: int):
        try:
            _, can_edit = charges.charge_summary(workspace, access(request), repair_id)
        except RepairError as error:
            return failure(request, error)
        if not can_edit:
            return failure(request, RepairError(403, "forbidden", "Your account cannot add charges."))
        return page(request, repair_id)

    @router.post("")
    def charge_create(request: Request, repair_id: int,
                      form: Annotated[dict[str, str], Depends(charge_form)]):
        check_form(request, form, key, f"charge.create:{repair_id}")
        fields = {name: form.get(name, "") for name in TEXT_FIELDS}
        request_key = form.get("key", "")
        try:
            charges.create_charge(workspace, access(request), repair_id, parsed(fields), key=request_key)
        except RepairError as error:
            if error.status in (409, 422):
                return page(request, repair_id, details=fields, request_key=request_key,
                            error=error, status=error.status)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path
                                + "?saved=charge", status_code=303)

    @router.get("/{charge_id}/edit")
    def charge_edit(request: Request, repair_id: int, charge_id: int):
        try:
            charge, can_edit = charges.read_charge(workspace, access(request), repair_id, charge_id)
        except RepairError as error:
            return failure(request, error)
        if not can_edit:
            return failure(request, RepairError(403, "forbidden", "This charge cannot be changed."))
        return page(request, repair_id, charge=charge, details=form_values(charge), revision=charge.revision)

    @router.post("/{charge_id}/edit")
    def charge_correct(request: Request, repair_id: int, charge_id: int,
                       form: Annotated[dict[str, str], Depends(charge_form)]):
        check_form(request, form, key, f"charge.correct:{charge_id}")
        fields = {name: form.get(name, "") for name in (*TEXT_FIELDS, "reason")}
        raw_revision = form.get("revision", "")
        request_key = form.get("key", "")
        try:
            details = parsed(fields)
            charges.correct_charge(workspace, access(request), repair_id, charge_id,
                                   ChargePatch(**vars(details) | {"revision": revision_of(raw_revision),
                                                                  "reason": fields["reason"]}),
                                   key=request_key)
        except RepairError as error:
            if error.status in (409, 422):
                try:
                    charge, can_edit = charges.read_charge(workspace, access(request), repair_id, charge_id)
                except RepairError as missing:
                    return failure(request, missing)
                if not can_edit:
                    return failure(request, error)
                return page(request, repair_id, charge=charge, details=fields, revision=raw_revision,
                            request_key=request_key, error=error, status=error.status)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path
                                + "?saved=charge", status_code=303)

    @router.post("/{charge_id}/visibility")
    def charge_visibility(request: Request, repair_id: int, charge_id: int,
                          form: Annotated[dict[str, str], Depends(charge_form)]):
        check_form(request, form, key, f"charge.visibility:{charge_id}")
        raw_revision = form.get("revision", "")
        try:
            charges.change_charge_visibility(
                workspace, access(request), repair_id, charge_id,
                ChargeVisibility(revision=revision_of(raw_revision), deleted=form.get("deleted") == "1",
                                 reason=form.get("reason", "")), key=form.get("key", ""))
        except RepairError as error:
            if error.status in (409, 422):
                try:
                    charge, can_edit = charges.read_charge(workspace, access(request), repair_id, charge_id)
                except RepairError as missing:
                    return failure(request, missing)
                if not can_edit:
                    return failure(request, error)
                return page(request, repair_id, charge=charge, details=form_values(charge),
                            revision=charge.revision, error=error, status=error.status,
                            visibility_key=form.get("key", ""))
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path
                                + "?saved=charge", status_code=303)

    def return_page(request, repair_id, charge_id, review, *, error=None, attempted=None, form=None):
        forms = {}
        for action in ("return_part_submit", "intake_mistake_submit"):
            values = form if action == attempted else {}
            forms[action] = {
                "csrf": form_token(request, key, f"{action}:{charge_id}"),
                "key": values.get("key", uuid4().hex),
                "repair_revision": values.get("repair_revision", review.repair_revision),
                "item_revision": values.get("item_revision", review.item_revision),
                "charge_revision": values.get("charge_revision", review.charge_revision),
                "quantity": values.get("quantity", ""),
                "reason": values.get("reason", ""),
            }
        return templates.TemplateResponse(request, "return_part.html", {
            "repair_id": repair_id, "charge_id": charge_id, "review": review, "forms": forms,
            "error": str(error) if error else "", "field_errors": web_errors(error) if error else {},
            "attempted": attempted,
        }, status_code=error.status if error else 200)

    @router.get("/{charge_id}/return-part")
    def return_part_review(request: Request, repair_id: int, charge_id: int):
        try:
            review = stock.part_return_status(workspace, access(request), repair_id, charge_id)
        except RepairError as error:
            return failure(request, error)
        return return_page(request, repair_id, charge_id, review)

    def submit_return(request, repair_id, charge_id, form, *, combined):
        action = "intake_mistake_submit" if combined else "return_part_submit"
        check_form(request, form, key, f"{action}:{charge_id}")
        revisions = {
            name: revision_of(form.get(name, "")) for name in ("repair_revision", "item_revision")
        }
        reason, request_key = form.get("reason", ""), form.get("key", "")
        try:
            if combined:
                change = IntakeMistake(**revisions, charge_revision=revision_of(form.get("charge_revision", "")),
                                       reason=reason)
                stock.correct_intake_mistake(workspace, access(request), repair_id, charge_id, change, key=request_key)
            else:
                try:
                    quantity = money.parse_milli(form.get("quantity", ""))
                except ValueError as error:
                    raise RepairError(422, "validation", "Check the return quantity.",
                                      {"quantity": str(error)}) from None
                stock.return_part(workspace, access(request), repair_id, charge_id,
                                  PartReturn(**revisions, quantity_milli=quantity, reason=reason), key=request_key)
        except RepairError as error:
            if error.status in (409, 422):
                try:
                    review = stock.part_return_status(workspace, access(request), repair_id, charge_id)
                except RepairError as missing:
                    return failure(request, missing)
                return return_page(request, repair_id, charge_id, review, error=error, attempted=action, form=form)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path, status_code=303)

    @router.post("/{charge_id}/return-part")
    def return_part_submit(request: Request, repair_id: int, charge_id: int,
                           form: Annotated[dict[str, str], Depends(charge_form)]):
        return submit_return(request, repair_id, charge_id, form, combined=False)

    @router.post("/{charge_id}/intake-mistake")
    def intake_mistake_submit(request: Request, repair_id: int, charge_id: int,
                              form: Annotated[dict[str, str], Depends(charge_form)]):
        return submit_return(request, repair_id, charge_id, form, combined=True)

    return router
