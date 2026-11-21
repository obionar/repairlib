"""Server-rendered active shop settings with no process-control side effects."""

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
from repairlib import money
from repairlib.contracts import PrinterChange, SettingsPatch
from repairlib.domain import printing, settings
from repairlib.domain.settings import SettingsError


async def printer_form(request: Request):
    return await decode_form(request, max_fields=4, max_bytes=4096)


def form_revision(form, field="revision") -> int:
    return form_int(form.get(field, ""))


async def settings_form(request: Request):
    return await decode_form(request, max_fields=12, max_bytes=16 * 1024)


def settings_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/settings", dependencies=[Depends(require_runtime)])

    def render(request, current=None, *, status=200, fields=None, request_key=None,
               error=None, field_errors=None):
        return templates.TemplateResponse(request, "settings.html", {
            "current": current, "details": fields or {}, "error": str(error) if error else "",
            "field_errors": field_errors or {}, "request_key": request_key or uuid4().hex,
            "revision": current.revision if current else (fields or {}).get("revision", ""),
            "csrf": form_token(request, key, "settings.update"),
        }, status_code=status)

    def failure(request, error):
        return refusal(request, error, lambda r, e: render(r, status=e.status, error=e))

    @router.get("")
    def settings_page(request: Request):
        try:
            current, diagnosis = settings.read_settings_page(workspace, access(request))
        except SettingsError as error:
            return failure(request, error)
        fields = {"name": current.name, "timezone": current.timezone, "revision": current.revision,
                  "language": current.language, "max_upload_mib": current.max_upload_mib,
                  "inline_preview_mib": current.inline_preview_mib, "address": current.address,
                  "phone": current.phone, "terms": current.terms, "diagnosis_fee": money.format_cents(diagnosis)}
        return render(request, current, fields=fields)

    @router.post("")
    def settings_save(request: Request, form: Annotated[dict[str, str], Depends(settings_form)]):
        check_form(request, form, key, "settings.update")
        fields = {name: form.get(name, "") for name in ("name", "timezone", "language",
                                                        "max_upload_mib", "inline_preview_mib",
                                                        "address", "phone", "terms", "diagnosis_fee")}
        revision = form.get("revision", "")
        reviewed = form_int(revision)
        request_key = form.get("key", "")
        limits = {name: None if name not in form else form_int(fields[name], 5)
                  for name in ("max_upload_mib", "inline_preview_mib")}
        try:
            try:
                diagnosis = money.parse_cents(fields["diagnosis_fee"].replace(",", ".")) if "diagnosis_fee" in form else None
            except ValueError as error:
                raise SettingsError(422, "validation", "Check the shop settings.",
                                    {"diagnosis_fee": str(error)}) from None
            settings.update_settings(workspace, access(request), SettingsPatch(
                revision=reviewed, name=form.get("name"), timezone=form.get("timezone"),
                language=fields["language"] or None,
                address=fields["address"] or None, phone=fields["phone"] or None,
                terms=fields["terms"] or None, **limits),
                key=request_key, diagnosis_fee_cents=diagnosis)
        except SettingsError as error:
            if error.status in (422, 409):
                return render(request, status=error.status, fields={**fields, "revision": revision},
                              request_key=request_key, error=error, field_errors=error.fields)
            return failure(request, error)
        return RedirectResponse(request.url_for("settings_page").path, status_code=303)

    @router.get("/printer")
    def printer_page(request: Request):
        try:
            current = printing.read_printer(workspace, access(request))
        except printing.RepairError as error:
            return failure(request, error)
        return templates.TemplateResponse(request, "printer.html", {
            "printer": current,
            "configure_csrf": form_token(request, key, "printer.configure"),
            "configure_key": uuid4().hex,
            "test_csrf": form_token(request, key, "printer.test"),
            "test_key": uuid4().hex,
        })

    @router.post("/printer")
    def printer_save(request: Request, form: Annotated[dict[str, str], Depends(printer_form)]):
        check_form(request, form, key, "printer.configure")
        change = PrinterChange(form.get("driver", ""), form_revision(form))
        try:
            printing.configure_printer(workspace, access(request), change, key=form.get("key", ""))
        except printing.RepairError as error:
            return failure(request, error)
        return RedirectResponse(request.url_for("printer_page").path, status_code=303)

    @router.post("/printer/test")
    def printer_test(request: Request, form: Annotated[dict[str, str], Depends(printer_form)]):
        check_form(request, form, key, "printer.test")
        try:
            receipt = printing.print_p15(workspace, access(request), printer_revision=form_revision(form),
                                         key=form.get("key", ""))
        except printing.RepairError as error:
            if error.status == 401:
                return failure(request, error)
            return templates.TemplateResponse(request, "print_result.html", {
                "message": str(error), "back": request.url_for("printer_page").path,
            }, status_code=error.status)
        return templates.TemplateResponse(request, "print_result.html", {
            "receipt": receipt, "message": receipt.message, "back": request.url_for("printer_page").path,
        })

    return router
