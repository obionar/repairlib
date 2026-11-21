"""Native repair intake, editing and work-entry forms."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import uuid4

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from repairlib.adapters.files import file_response
from repairlib.adapters.web.settings import form_revision, printer_form
from repairlib.adapters.web.uploads import receive_upload

from repairlib.adapters.web.errors import PAGES, error_page
from repairlib.adapters.web.security import (
    browser_access as access,
    check_csrf, csrf_token, decode_form, form_int, require_runtime,
)
from repairlib import money
from repairlib.contracts import (
    AttachmentCaption, AttachmentDetach, AttachmentLink, AttachmentPurge, ChargeInput, PartConsumption,
    RepairFlagChange, RepairInput, RepairPatch, WorkEntryCorrection, WorkEntryInput,
    WorkEntryVisibility,
)
from repairlib.domain import attachments, charges, customers, inventory, printing, repairs, stock
from repairlib.domain.repairs import RepairError


async def repair_form(request: Request):
    return await decode_form(request, max_fields=14, max_bytes=32 * 1024)


async def entry_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=16 * 1024)


SAVED_NOTICES = {
    "stage": "Repair status updated.", "charge": "Charge saved.",
    "entry": "Work entry added.", "part": "Part added.", "entry_corrected": "Work entry corrected.",
    "entry_visibility": "Work entry updated.", "pickup": "Pickup state saved.",
    "archive": "Archive state saved.", "attachment": "File saved.", "payment": "Payment status saved.",
    "handover": "Handed over: paid and picked up.",
    "fees": "Amounts saved.",
}
NOTICE_PANELS = {
    "stage": "status", "archive": "status", "charge": "payment-total",
    "entry": "log", "part": "stock-usage", "entry_corrected": "log", "entry_visibility": "log",
    "attachment": "files", "pickup": "status", "payment": "status", "handover": "status", "fees": "payment-total",
}
CURSOR_FIELDS = ("log_before", "files_before", "detached_before")


def cursor_query(form: dict[str, str]) -> str:
    return urlencode({name: value for name in CURSOR_FIELDS
                      if (value := cursor_value(form, name)) is not None})


def cursor_value(form: dict[str, str], name: str) -> int | None:
    value = form_int(form.get(name, ""), default=-1)
    return value if 0 <= value < 1 << 63 else None


def back(request, repair_id: int, anchor: str, form: dict[str, str], *, saved: str) -> RedirectResponse:
    query = "&".join(part for part in (f"saved={saved}", cursor_query(form)) if part)
    location = request.url_for("repair_detail", repair_id=repair_id).path
    return RedirectResponse(f"{location}?{query}#{anchor}", status_code=303)


async def stage_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=16 * 1024)


async def fees_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=4096)


async def flag_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=16 * 1024)


async def consumption_form(request: Request):
    return await decode_form(request, max_fields=8, max_bytes=32 * 1024)


def consumption_amounts(form: dict[str, str]) -> dict:
    values, errors = {}, {}
    for field, parse, target in (
        ("consumed_quantity", money.parse_milli, "consumed_quantity_milli"),
        ("billed_quantity", money.parse_milli, "billed_quantity_milli"),
        ("unit_price", money.parse_cents, "unit_price_cents"),
    ):
        try:
            raw = form.get(field, "").strip()
            if field == "unit_price" and not raw:
                values[target] = None
            else:
                values[target] = parse(raw or (form.get("consumed_quantity", "") if field == "billed_quantity" else ""))
        except ValueError as error:
            errors[field] = str(error)
    if errors:
        raise RepairError(422, "validation", "Check the part quantities and price.", errors)
    return values


def optional_id(value: str) -> int | None:
    return form_int(value) if value else None


def repair_routes(workspace: Path, templates, key: bytearray) -> APIRouter:
    router = APIRouter(prefix="/repairs", dependencies=[Depends(require_runtime)])

    def render(request, template="repairs.html", *, status=200, **context):
        return templates.TemplateResponse(request, template, context, status_code=status)

    def failure(request, error):
        if error.status == 401:
            return RedirectResponse(request.url_for("sign_in").path, status_code=303)
        if error.status in PAGES:
            return error_page(request, error.status, str(error))
        return render(request, status=error.status, error=str(error))

    def reviewed_repair(request, repair_id):
        repair, _, can_write = repairs.read_repair(workspace, access(request), repair_id)
        if not can_write:
            raise RepairError(403, "forbidden", "Your account or token cannot change repairs.")
        if repair.archived_at is not None:
            raise RepairError(409, "archived", "Restore the archived repair before changing it.")
        return repair

    @router.get("/{repair_id}/consume-part")
    def part_picker(request: Request, repair_id: int, before: int | None = None, q: str = ""):
        try:
            repair = reviewed_repair(request, repair_id)
        except RepairError as error:
            return failure(request, error)
        try:
            items = inventory.list_items(workspace, access(request), before=before, query=q)
        except RepairError as error:
            if error.status != 422:
                return failure(request, error)
            return render(request, "consume_part.html", status=422, repair=repair, q=q, error=error)
        return render(request, "consume_part.html", repair=repair, page=items, before=before, q=q)

    @router.post("/{repair_id}/consume-part")
    def use_manual_part(request: Request, repair_id: int,
                        form: Annotated[dict[str, str], Depends(consumption_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"repair.manual_part:{repair_id}", credential)
                   if credential else None)
        draft = {name: form.get(name, "") for name in ("description", "quantity", "unit_price")}
        try:
            try:
                quantity = money.parse_milli(draft["quantity"].replace(",", "."))
                price = money.parse_cents(draft["unit_price"].replace(",", "."))
            except ValueError as error:
                raise RepairError(422, "validation", str(error)) from None
            charges.create_charge(workspace, access(request), repair_id, ChargeInput(
                kind="part", description=draft["description"], quantity_milli=quantity, unit_price_cents=price,
            ), key=form.get("key", ""), part_revision=form_revision(form))
        except RepairError as error:
            if error.status in (409, 422):
                return detail_page(request, repair_id, status=error.status, manual=draft, manual_error=error)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path + "?saved=part",
                                status_code=303)

    def part_page(request, repair_id, item_id, *, before=None, q="", form=None, error=None):
        try:
            repair = reviewed_repair(request, repair_id)
            item = inventory.read_item(workspace, access(request), item_id)
            if not item.active:
                raise RepairError(409, "archived", "The inventory item is archived.")
        except RepairError as review_error:
            return failure(request, review_error)
        if form is None:
            form = {"repair_revision": str(repair.revision), "item_revision": str(item.revision),
                    "consumed_quantity": "1"}
        field_names = {"consumed_quantity_milli": "consumed_quantity", "billed_quantity_milli": "billed_quantity",
                       "unit_price_cents": "unit_price", "body": "work_note"}
        field_errors = {field_names.get(name, name): message
                        for name, message in ((error.fields or {}).items() if error else ())}
        return render(request, "consume_part.html", status=error.status if error else 200,
                      repair=repair, item=item, form=form, error=error, field_errors=field_errors,
                      q=q, before=before, part_work_log=True,
                      request_key=form.get("key") or uuid4().hex,
                      csrf=csrf_token(key, f"inventory.consume:{repair_id}:{item_id}",
                                      access(request).credential))

    @router.get("/{repair_id}/consume-part/{item_id}")
    def review_part(request: Request, repair_id: int, item_id: int, before: int | None = None, q: str = ""):
        return part_page(request, repair_id, item_id, before=before, q=q)

    @router.post("/{repair_id}/consume-part/{item_id}")
    def consume_part(request: Request, repair_id: int, item_id: int,
                     form: Annotated[dict[str, str], Depends(consumption_form)],
                     before: int | None = None, q: str = ""):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"inventory.consume:{repair_id}:{item_id}", credential)
                   if credential else None)
        try:
            stock.consume_for_repair(workspace, access(request), repair_id, PartConsumption(
                item_id=item_id, repair_revision=form_revision(form, "repair_revision"),
                item_revision=form_revision(form, "item_revision"), **consumption_amounts(form),
            ), key=form.get("key", ""), work_note=form.get("work_note", ""))
        except RepairError as error:
            if error.status in (409, 422):
                return part_page(request, repair_id, item_id, before=before, q=q, form=form, error=error)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path + "#log",
                                status_code=303)

    def draft(request, *, fields=None, request_key=None, error=None, repair_id=None, revision=None,
              customer_receipt="", history_serial=None):
        fields = fields or {}
        try:
            customer, customer_options = repairs.repair_options(
                workspace, access(request), repair_id, customer_id=fields.get("customer_id"),
            )
            notice = customers.read_create_notice(workspace, access(request), fields.get("customer_id"),
                                                  customer_receipt) if customer else None
        except RepairError as auth_error:
            return failure(request, auth_error)
        if fields.get("customer_id") is not None and customer is None and error is None:
            return failure(request, RepairError(404, "not_found", "Customer not found."))
        history = None
        history_error = ""
        if history_serial is not None:
            try:
                history = repairs.device_history(workspace, access(request), history_serial)
            except RepairError as lookup_error:
                if lookup_error.status != 422:
                    return failure(request, lookup_error)
                history_error = lookup_error.fields["serial"]
        purpose = f"repair.update:{repair_id}" if repair_id is not None else "repair.create"
        return render(
            request, "repair_form.html", status=error.status if error else 200,
            details=fields, limits=repairs.FIELD_LIMITS, customers=customer_options, customer=customer,
            contact_warnings=notice.contact_warnings if notice else {},
            possible_duplicate_id=notice.possible_duplicate_id if notice else None,
            csrf=csrf_token(key, purpose, access(request).credential),
            request_key=request_key or uuid4().hex, error=str(error) if error else "",
            field_errors=(error.fields or {}) if error else {}, repair_id=repair_id, revision=revision,
            stale=error is not None and error.code == "stale_revision",
            customer_receipt=customer_receipt, history=history, history_error=history_error,
        )

    def detail_page(request, repair_id, *, status=200, stage_error=None, stage_fields=None,
                    stage_key=None, stage_revision=None, flag_error=None, flag_operation=None,
                    attachment_error=None, attachment_key=None, photo_error=None,
                    metadata_draft=None, entry_error=None, entry_fields=None, manual=None, manual_error=None,
                    fees_error=None, fees_draft=None,
                    log_before=None, files_before=None, detached_before=None,
                    activity_before=None, show_activity=False, part_q=""):
        try:
            record, names, can_write = repairs.read_repair(workspace, access(request), repair_id,
                                                          customer_context=True)
            activity = repairs.repair_activity(workspace, access(request), repair_id, before=activity_before)
            entries, _ = repairs.list_work_entries(workspace, access(request), repair_id,
                                                   before=log_before)
            files, _ = attachments.list_attachments(workspace, access(request), repair_id,
                                                    before=files_before, detached=False)
            detached_files, _ = attachments.list_attachments(workspace, access(request), repair_id,
                                                             before=detached_before, detached=True)
            summary, used_parts = charges.work_log_billing(workspace, access(request), repair_id)
            fees, fees_can_write = charges.ticket_fees(workspace, access(request), repair_id)
            upload_mib, is_admin = attachments.upload_context(workspace, access(request))
            photo_id, entry_pictures = attachments.ticket_pictures(workspace, access(request), repair_id)
        except RepairError as error:
            return failure(request, error)
        credential = access(request).credential
        part_q = part_q.strip()
        part_results, part_error = None, ""
        if part_q and can_write and record.archived_at is None:
            try:
                part_results = inventory.list_items(workspace, access(request), limit=10, query=part_q)
            except RepairError as error:
                if error.status != 422:
                    return failure(request, error)
                part_error = str(error)
        fields = stage_fields or {}
        stage_conflict = None
        stale_stage = stage_error is not None and stage_error.code == "stale_revision"
        pickup_draft = fields.get("pickup", "")
        if pickup_draft == ("1" if record.picked_up_at else "0"):
            pickup_draft = ""
        payment_draft = fields.get("payment", "")
        if names["payment_paid"] is not None and payment_draft == str(int(names["payment_paid"])):
            payment_draft = ""
        stage_matches = stale_stage and fields.get("stage") in ("", record.stage) and not (pickup_draft or payment_draft)
        if stale_stage and not stage_matches:
            stage_conflict = {
                "saved_stage": record.stage,
                "draft_stage": fields.get("stage", ""),
                "draft_reason": fields.get("reason", ""),
                "options": repairs.stage_options(record.stage),
                "revision": record.revision,
                "key": uuid4().hex,
            }
        error_summary = []
        if entry_error:
            error_summary.append({
                "message": str(entry_error), "href": "#entry-body",
                "fields": {("entry-body" if name == "body" else f"entry-{name}"): message
                           for name, message in (entry_error.fields or {}).items()},
            })
        if attachment_error:
            error_summary.append({
                "message": str(attachment_error), "href": "#files",
                "fields": {"files": message for message in (attachment_error.fields or {}).values()},
            })
        if flag_error:
            error_summary.append({
                "message": str(flag_error),
                "href": "#pickup" if flag_operation == "pickup" else "#status",
                "fields": {},
            })
        def panel_url(**updates):
            cursors = {name: value for name, value in (("log_before", log_before),
                                                       ("files_before", files_before),
                                                       ("detached_before", detached_before),
                                                       ("activity_before", activity_before),
                                                       ("show_activity", 'true' if show_activity else None)) if value}
            cursors.update(updates)
            cursors = {name: value for name, value in cursors.items() if value}
            location = request.url_for("repair_detail", repair_id=repair_id).path
            return location + ("?" + urlencode(cursors) if cursors else "")

        saved = request.query_params.get("saved", "")
        return render(
            request, "repair_detail.html", status=status, repair=record, names=names,
            can_write=can_write, can_edit=can_write and record.archived_at is None,
            notice=SAVED_NOTICES.get(saved), notice_panel=NOTICE_PANELS.get(saved), created=saved == "created",
            error_summary=error_summary, activity=activity,
            activity_open=bool(activity_before or show_activity),
            activity_older=panel_url(activity_before=activity.next_before) if activity.next_before else "",
            activity_newest=panel_url(activity_before=None, show_activity='true') if activity_before else "",
            log_paged=bool(entries.next_before or log_before),
            files_paged=bool(files.next_before or files_before),
            detached_paged=bool(detached_files.next_before or detached_before),
            entries=entries, stages=repairs.STAGE_TRANSITIONS, stage_options=repairs.stage_options(record.stage),
            files_before=files_before or 0, detached_before=detached_before or 0,
            log_before=log_before or 0,
            log_older=panel_url(log_before=entries.next_before) if entries.next_before else "",
            log_newest=panel_url(log_before=None),
            files_older=panel_url(files_before=files.next_before) if files.next_before else "",
            files_newest=panel_url(files_before=None),
            detached_older=panel_url(detached_before=detached_files.next_before)
            if detached_files.next_before else "",
            detached_newest=panel_url(detached_before=None),
            entry_error=str(entry_error) if entry_error else "",
            entry_error_fields=(entry_error.fields or {}) if entry_error else {},
            entry_draft=entry_fields or {},
            stage_error=str(stage_error) if stage_error else "",
            stage_matches=stage_matches, stage_conflict=stage_conflict, pickup_draft=pickup_draft,
            stage_draft=fields.get("stage", ""),
            payment_draft=payment_draft, handover_key=uuid4().hex, handover_available=record.stage in repairs.TERMINAL_REPAIR_STAGES,
            stage_key=uuid4().hex if stage_matches else stage_key or uuid4().hex,
            stage_revision=record.revision if stage_revision is None or stage_matches else stage_revision,
            stage_csrf=csrf_token(key, f"repair.stage:{repair_id}", credential),
            archive_key=uuid4().hex,
            archive_csrf=csrf_token(key, f"repair.archive:{repair_id}", credential),
            flag_error=str(flag_error) if flag_error else "", flag_operation=flag_operation,
            entry_csrf=csrf_token(key, f"entry.create:{repair_id}", credential),
            entry_key=uuid4().hex,
            entry_correct_csrf={item.id: csrf_token(key, f"entry.correct:{item.id}", credential)
                                for item in entries.items},
            entry_visibility_csrf={item.id: csrf_token(key, f"entry.visibility:{item.id}", credential)
                                   for item in entries.items},
            entry_correct_key={item.id: uuid4().hex for item in entries.items},
            entry_visibility_key={item.id: uuid4().hex for item in entries.items},

            files=files, detached_files=detached_files,
            attachment_error=str(attachment_error) if attachment_error else "",
            attachment_key=attachment_key or uuid4().hex,
            photo_id=photo_id, entry_pictures=entry_pictures, photo_key=uuid4().hex,
            photo_error=str(photo_error) if photo_error else "",
            attachment_csrf=csrf_token(key, f"attachment.create:{repair_id}", credential),
            caption_csrf=csrf_token(key, f"attachment.caption:{repair_id}", credential),
            link_csrf=csrf_token(key, f"attachment.link:{repair_id}", credential),
            metadata_draft=metadata_draft,
            detach_csrf=csrf_token(key, f"attachment.detach:{repair_id}", credential),
            max_upload_mib=upload_mib, is_admin=is_admin,
            purge_csrf=csrf_token(key, f"attachment.purge:{repair_id}", credential),
            purge_cutoff=attachments.utc_text(datetime.now(UTC) - attachments.PURGE_AFTER),
            summary=summary, used_parts=used_parts,
            fees=fees, fees_draft=fees_draft or {},
            fees_error=str(fees_error) if fees_error else "", fees_can_write=fees_can_write,
            fees_key=uuid4().hex, fees_csrf=csrf_token(key, f"repair.fees:{repair_id}", credential),
            part_q=part_q, part_results=part_results, part_error=part_error,
            part_csrf={item.id: csrf_token(key, f"inventory.consume:{repair_id}:{item.id}", credential)
                       for item in (part_results.items if part_results else [])},
            part_keys={item.id: uuid4().hex for item in (part_results.items if part_results else [])},
            manual=({"quantity": "1", "description": part_q if part_results and not part_results.items else ""}
                    if manual is None else manual),
            manual_error=str(manual_error) if manual_error else "", manual_revision=record.revision,
            manual_key=uuid4().hex, manual_csrf=csrf_token(key, f"repair.manual_part:{repair_id}", credential),
        )

    @router.get("")
    def repair_list(request: Request, before: int | None = None, q: str = "", stage: str = "",
                    picked_up: Literal["", "true", "false"] = "", archived: Literal["", "true", "false"] = "",
                    technician: str = "", customer_id: int | None = None, queue: str = "",
                    paid: Literal["", "true", "false"] = ""):
        actor = access(request)
        filters = {"q": q, "stage": stage, "picked_up": picked_up, "paid": paid, "archived": archived,
                   "technician": technician, "customer_id": customer_id, "queue": queue}
        filters = {name: value for name, value in filters.items() if value not in ("", None)}
        try:
            page, can_write = repairs.list_repairs(
                workspace, actor, before=before, query=q, stage=stage or None,
                picked_up=picked_up == "true" if picked_up else None,
                archived=archived == "true" if archived else None,
                paid=paid == "true" if paid else None,
                technician=technician, customer_id=customer_id, queue=queue,
            )
        except RepairError as error:
            return failure(request, error)
        return render(request, page=page, can_write=can_write, before=before, filters=filters,
                      stages=repairs.STAGE_TRANSITIONS)

    def customer_picker(request, query, before, repair_id=None):
        actor = access(request)
        try:
            if not customers.may_write(workspace, actor):
                raise RepairError(403, "forbidden", "Your account or token cannot change repairs.")
            page, _ = customers.list_customers(workspace, actor, before=before, limit=10, query=query)
        except RepairError as error:
            if error.status != 422:
                return failure(request, error)
            return render(request, "repair_customer_picker.html", status=422, query=query,
                          repair_id=repair_id, error=str(error))
        return render(request, "repair_customer_picker.html", query=query, page=page, before=before,
                      repair_id=repair_id)

    @router.get("/new")
    def repair_new(request: Request, customer_id: int | None = None, customer_q: str = "",
                   before: int | None = None, customer_receipt: str = "", serial: str | None = None):
        if customer_id is None:
            return customer_picker(request, customer_q, before)
        return draft(request, fields={"customer_id": customer_id or None, "serial": (serial or "").strip()},
                     customer_receipt=customer_receipt, history_serial=serial)

    @router.post("/new")
    def repair_create(request: Request, form: Annotated[dict[str, str], Depends(repair_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, "repair.create", credential) if credential else None)
        fields = {name: form.get(name, "") for name in repairs.FIELD_LIMITS}
        fields["warranty_until"] = form.get("warranty_until")
        fields["warranty_repair_id"] = optional_id(form.get("warranty_repair_id", ""))
        fields.update(customer_id=optional_id(form.get("customer_id", "")),
                      technician_id=optional_id(form.get("technician_id", "")))
        request_key = form.get("key", "")
        if form.get("lookup_history") == "1":
            return draft(request, fields=fields, request_key=request_key,
                         customer_receipt=form.get("customer_receipt", ""), history_serial=fields["serial"])
        try:
            created = repairs.create_repair(workspace, access(request), RepairInput(**fields), key=request_key)
        except RepairError as error:
            if error.status in (422, 409):
                return draft(request, fields=fields, request_key=request_key, error=error)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=created.id).path + "?saved=created#status",
                                status_code=303)

    @router.get("/{repair_id}")
    def repair_detail(request: Request, repair_id: int, log_before: int | None = None,
                      files_before: int | None = None, detached_before: int | None = None,
                      activity_before: int | None = None, show_activity: bool = False, part_q: str = ""):
        return detail_page(request, repair_id, log_before=log_before, files_before=files_before,
                           detached_before=detached_before, activity_before=activity_before,
                           show_activity=show_activity, part_q=part_q[:100])

    @router.post("/{repair_id}/stage")
    def repair_stage(request: Request, repair_id: int,
                     form: Annotated[dict[str, str], Depends(stage_form)]):
        credential = access(request).credential
        purpose = f"repair.stage:{repair_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        raw_revision = form.get("revision", "")
        reviewed = form.get("reviewed") == "1"
        revision = form_int(form.get("review_revision", "") if reviewed else raw_revision)
        fields = {"stage": form.get("stage", ""), "reason": form.get("reason", "")}
        pickup = form.get("pickup", "")
        payment = form.get("payment", "")
        request_key = form.get("key", "")
        try:
            if pickup not in ("", "0", "1"):
                raise RepairError(422, "validation", "Choose a valid pickup status.")
            if payment not in ("", "0", "1"):
                raise RepairError(422, "validation", "Choose a valid payment status.")
            if not fields["stage"] and not pickup and not payment:
                raise RepairError(422, "validation", "Choose a status to update.")
            repairs.change_ticket_status(workspace, access(request), repair_id,
                                                   revision=revision, **fields, key=request_key,
                                                   pickup=(pickup == "1") if pickup else None,
                                                   payment=(payment == "1") if payment else None)
        except RepairError as error:
            if error.status in (422, 409):
                return detail_page(request, repair_id, status=error.status, stage_error=error,
                                   stage_fields={**fields, "pickup": pickup if pickup in ("0", "1") else "",
                                                 "payment": payment if payment in ("0", "1") else ""}, stage_key=request_key,
                                   stage_revision=raw_revision)
            return failure(request, error)
        query = "saved=handover" if form.get("handover") == "1" else "saved=pickup" if pickup else "saved=payment" if payment else "saved=stage"
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path
                                + f"?{query}", status_code=303)

    @router.post("/{repair_id}/fees")
    def repair_fees(request: Request, repair_id: int, form: Annotated[dict[str, str], Depends(fees_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"repair.fees:{repair_id}", credential) if credential else None)
        draft = {name: form.get(name, "") for name in ("diagnosis", "labor", "discount")}
        try:
            try:
                amounts = {name: money.parse_cents(value.replace(",", ".") or "0")
                           for name, value in draft.items()}
            except ValueError as error:
                raise RepairError(422, "validation", str(error)) from None
            charges.set_fees(workspace, access(request), repair_id, diagnosis=amounts["diagnosis"],
                             labor=amounts["labor"], discount=amounts["discount"],
                             revision=form_revision(form), key=form.get("key", ""))
        except RepairError as error:
            if error.status in (409, 422):
                return detail_page(request, repair_id, status=error.status, fees_error=error, fees_draft=draft)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path + "?saved=fees",
                                status_code=303)

    def submit_flag(request, repair_id, form, flag):
        credential = access(request).credential
        purpose = f"repair.{flag}:{repair_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        revision = form_revision(form)
        try:
            if flag == "pickup" and form.get("enabled") not in ("0", "1"):
                raise RepairError(422, "validation", "Choose a valid pickup status.")
            change = RepairFlagChange(revision=revision, enabled=form.get("enabled") == "1")
            repairs.change_repair_flag(workspace, access(request), repair_id, change,
                                               key=form.get("key", ""), flag=flag)
        except RepairError as error:
            if error.status in (422, 409):
                return detail_page(request, repair_id, status=error.status,
                                   flag_error=error, flag_operation=flag)
            return failure(request, error)
        anchor = "#pickup" if flag == "pickup" else "#status"
        query = f"saved={flag}"
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path
                                + f"?{query}{anchor}", status_code=303)

    @router.post("/{repair_id}/pickup")
    def repair_pickup(request: Request, repair_id: int,
                      form: Annotated[dict[str, str], Depends(flag_form)]):
        return submit_flag(request, repair_id, form, "pickup")

    @router.post("/{repair_id}/archive")
    def repair_archive(request: Request, repair_id: int,
                       form: Annotated[dict[str, str], Depends(flag_form)]):
        return submit_flag(request, repair_id, form, "archive")

    @router.post("/{repair_id}/attachments")
    async def attachment_upload(request: Request, repair_id: int):
        return await receive_attachment(request, repair_id)

    @router.post("/{repair_id}/photo")
    async def repair_photo(request: Request, repair_id: int):
        return await receive_attachment(request, repair_id, role="device")

    async def receive_attachment(request: Request, repair_id: int, *, role: str = ""):
        error_name = "photo_error" if role else "attachment_error"
        credential = access(request).credential
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "Form origin is invalid.")
        try:
            declared = attachments.check_declared_length(request.headers.get("content-length"))
            staged = await run_in_threadpool(attachments.prepare_upload, workspace, access(request),
                                             repair_id, "upload", None, declared_size=declared)
        except RepairError as error:
            if error.status == 413:
                return await run_in_threadpool(lambda: detail_page(request, repair_id, status=413,
                                                                   **{error_name: error}))
            return failure(request, error)
        fields = {}
        try:
            fields, filename = await receive_upload(request, staged)
            check_csrf(request, fields, csrf_token(key, f"attachment.create:{repair_id}", credential))
            if filename is None:
                raise RepairError(422, "validation", "Choose a file to attach.")
            entry_id = optional_id(fields.get("entry_id", ""))
        except BaseException as error:
            staged.discard()
            if isinstance(error, RepairError):
                return await run_in_threadpool(lambda: detail_page(
                    request, repair_id, status=error.status, attachment_key=fields.get("key"),
                    **{error_name: error}))
            raise
        request_key = fields.get("key", "")
        try:
            await run_in_threadpool(lambda: attachments.commit_upload(
                workspace, access(request), repair_id, staged, filename=filename,
                entry_id=entry_id, key=request_key, role=role))
        except RepairError as error:
            if error.status in (409, 413, 422):
                return await run_in_threadpool(lambda: detail_page(
                    request, repair_id, status=error.status, attachment_key=request_key,
                    **{error_name: error}))
            return failure(request, error)
        return back(request, repair_id, "status" if role else "files", fields, saved="attachment")

    @router.get("/{repair_id}/attachments/{attachment_id}/content")
    def attachment_content(request: Request, repair_id: int, attachment_id: int, inline: bool = False):
        try:
            found, source = attachments.attachment_content(workspace, access(request), repair_id,
                                                            attachment_id, inline=inline)
        except RepairError as error:
            return failure(request, error)
        return file_response(found, source, inline=inline,
                             if_none_match=request.headers.get("if-none-match"))

    def save_attachment_metadata(request, repair_id, attachment_id, form, action, change, mutate):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"attachment.{action}:{repair_id}", credential)
                   if credential else None)
        try:
            mutate(workspace, access(request), repair_id, attachment_id, change, key=form.get("key", ""))
        except RepairError as error:
            if error.status in (409, 422):
                return detail_page(request, repair_id, status=error.status, attachment_error=error,
                                   metadata_draft={**form, "attachment_id": attachment_id,
                                                   "action": action, "error": error})
            return failure(request, error)
        return back(request, repair_id, f"attachment-{attachment_id}", form, saved="attachment")

    @router.post("/{repair_id}/attachments/{attachment_id}/caption")
    def attachment_caption(request: Request, repair_id: int, attachment_id: int,
                           form: Annotated[dict[str, str], Depends(flag_form)]):
        change = AttachmentCaption(optional_id(form.get("revision", "")) or 0, form.get("caption", ""))
        return save_attachment_metadata(request, repair_id, attachment_id, form, "caption", change,
                                        attachments.edit_caption)

    @router.post("/{repair_id}/attachments/{attachment_id}/links")
    def attachment_links(request: Request, repair_id: int, attachment_id: int,
                         form: Annotated[dict[str, str], Depends(stage_form)]):
        linked = form.get("linked")
        if linked not in {"0", "1"}:
            raise HTTPException(422, "Choose whether to link or unlink the attachment.")
        change = AttachmentLink(optional_id(form.get("revision", "")) or 0,
                                optional_id(form.get("entry_id", "")) or 0, linked == "1")
        return save_attachment_metadata(request, repair_id, attachment_id, form, "link", change,
                                        attachments.change_entry_link)

    @router.post("/{repair_id}/attachments/{attachment_id}/detach")
    def attachment_detach(request: Request, repair_id: int, attachment_id: int,
                          form: Annotated[dict[str, str], Depends(flag_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"attachment.detach:{repair_id}", credential)
                   if credential else None)
        revision = form_revision(form)
        try:
            attachments.change_detached(workspace, access(request), repair_id, attachment_id,
                                        AttachmentDetach(revision=revision,
                                                         detached=form.get("detached") == "1"),
                                        key=form.get("key", ""))
        except RepairError as error:
            if error.status in (422, 409):
                return detail_page(request, repair_id, status=error.status, attachment_error=error)
            return failure(request, error)
        return back(request, repair_id, "files", form, saved="attachment")

    @router.post("/{repair_id}/attachments/{attachment_id}/purge")
    def attachment_purge(request: Request, repair_id: int, attachment_id: int,
                         form: Annotated[dict[str, str], Depends(flag_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"attachment.purge:{repair_id}", credential)
                   if credential else None)
        revision = form_revision(form)
        try:
            attachments.purge_attachment(workspace, access(request), repair_id, attachment_id,
                                         AttachmentPurge(revision=revision), key=form.get("key", ""))
        except RepairError as error:
            if error.status in (422, 409):
                return detail_page(request, repair_id, status=error.status, attachment_error=error)
            return failure(request, error)
        return back(request, repair_id, "files", form, saved="attachment")

    @router.get("/{repair_id}/print")
    def repair_print(request: Request, repair_id: int):
        try:
            view = printing.print_view(workspace, access(request), repair_id)
        except RepairError as error:
            return failure(request, error)
        return render(request, "print_summary.html", view=view)

    @router.get("/{repair_id}/label")
    def repair_label(request: Request, repair_id: int):
        try:
            view = printing.print_view(workspace, access(request), repair_id, label=True)
        except RepairError as error:
            return failure(request, error)
        return render(request, "print_label.html", view=view, print_key=uuid4().hex,
                      print_csrf=csrf_token(key, f"printer.label:{repair_id}", access(request).credential))

    @router.post("/{repair_id}/label")
    def repair_label_send(request: Request, repair_id: int,
                          form: Annotated[dict[str, str], Depends(printer_form)]):
        credential = access(request).credential
        check_csrf(request, form, csrf_token(key, f"printer.label:{repair_id}", credential)
                   if credential else None)
        back = request.url_for("repair_label", repair_id=repair_id).path
        try:
            receipt = printing.print_p15(
                workspace, access(request), repair_id=repair_id, repair_revision=form_revision(form),
                printer_revision=form_revision(form, "printer_revision"), key=form.get("key", ""),
            )
        except RepairError as error:
            if error.status == 401:
                return failure(request, error)
            return render(request, "print_result.html", status=error.status, back=back, message=str(error))
        return render(request, "print_result.html", back=back, receipt=receipt, message=receipt.message)

    @router.get("/{repair_id}/edit")
    def repair_edit(request: Request, repair_id: int, customer_id: int | None = None,
                    customer_q: str | None = None, before: int | None = None):
        try:
            record = reviewed_repair(request, repair_id)
        except RepairError as error:
            return failure(request, error)
        if customer_q is not None:
            return customer_picker(request, customer_q, before, repair_id)
        fields = {name: getattr(record, name) for name in repairs.FIELD_LIMITS}
        fields.update(customer_id=record.customer_id if customer_id is None else customer_id or None,
                      technician_id=record.technician_id)
        return draft(request, fields=fields, repair_id=repair_id, revision=record.revision)

    @router.post("/{repair_id}/edit")
    def repair_save(request: Request, repair_id: int,
                    form: Annotated[dict[str, str], Depends(repair_form)]):
        credential = access(request).credential
        purpose = f"repair.update:{repair_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        fields = {name: form.get(name, "") for name in repairs.FIELD_LIMITS}
        if "warranty_until" in form:
            fields["warranty_until"] = form["warranty_until"]
        if "warranty_repair_id" in form:
            fields["warranty_repair_id"] = optional_id(form["warranty_repair_id"])
        fields.update(customer_id=optional_id(form.get("customer_id", "")),
                      technician_id=optional_id(form.get("technician_id", "")))
        raw_revision = form.get("revision", "")
        revision = form_int(raw_revision)
        request_key = form.get("key", "")
        try:
            repairs.update_repair(workspace, access(request), repair_id,
                                  RepairPatch(revision=revision, **fields), key=request_key,
                                  preserve_technician="technician_id" not in form)
        except RepairError as error:
            if error.status in (422, 409):
                return draft(request, fields=fields, request_key=request_key, error=error,
                             repair_id=repair_id, revision=raw_revision)
            return failure(request, error)
        return RedirectResponse(request.url_for("repair_detail", repair_id=repair_id).path, status_code=303)

    @router.get("/{repair_id}/entries/new")
    def entry_new(request: Request, repair_id: int):
        try:
            _, _, can_write = repairs.read_repair(workspace, access(request), repair_id)
        except RepairError as error:
            return failure(request, error)
        if not can_write:
            return failure(request, RepairError(403, "forbidden", "Your account cannot add work entries."))
        credential = access(request).credential
        return render(request, "entry_form.html", repair_id=repair_id, details={"kind": "work"},
                      request_key=uuid4().hex, csrf=csrf_token(key, f"entry.create:{repair_id}", credential),
                      field_errors={}, kinds=sorted(repairs.ENTRY_KINDS))

    @router.post("/{repair_id}/entries")
    async def entry_create(request: Request, repair_id: int):
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await entry_form(request)
            return await run_in_threadpool(save_entry, request, repair_id, form)
        credential = access(request).credential
        try:
            declared = attachments.check_declared_length(request.headers.get("content-length"))
            staged = await run_in_threadpool(attachments.prepare_upload, workspace, access(request),
                                             repair_id, "upload", None, declared_size=declared)
        except RepairError as error:
            return failure(request, error)
        try:
            form, filename = await receive_upload(request, staged)
            check_csrf(request, form, csrf_token(key, f"entry.create:{repair_id}", credential))
        except BaseException as error:
            staged.discard()
            if isinstance(error, RepairError):
                return await run_in_threadpool(lambda: detail_page(request, repair_id, status=error.status,
                                                                   entry_error=error))
            raise
        if not filename:  # the browser sends an empty file part when no picture was chosen
            staged.discard()
            return await run_in_threadpool(save_entry, request, repair_id, form)
        fields = {"kind": form.get("kind", ""), "body": form.get("body", "")}
        try:
            await run_in_threadpool(lambda: attachments.commit_upload(
                workspace, access(request), repair_id, staged, filename=filename, entry_id=None,
                key=form.get("key", ""), entry=WorkEntryInput(**fields)))
        except RepairError as error:
            if error.status in (409, 413, 422):
                return await run_in_threadpool(lambda: detail_page(
                    request, repair_id, status=error.status, entry_error=error, entry_fields=fields,
                    log_before=cursor_value(form, "log_before")))
            return failure(request, error)
        return back(request, repair_id, "log", form, saved="entry")

    def save_entry(request: Request, repair_id: int, form: dict[str, str]):
        credential = access(request).credential
        purpose = f"entry.create:{repair_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        fields = {"kind": form.get("kind", ""), "body": form.get("body", "")}
        request_key = form.get("key", "")
        try:
            repairs.create_work_entry(workspace, access(request), repair_id,
                                      WorkEntryInput(**fields), key=request_key)
        except RepairError as error:
            if error.status in (422, 409):
                return detail_page(request, repair_id, status=error.status, entry_error=error,
                                   entry_fields=fields, log_before=cursor_value(form, "log_before"))
            return failure(request, error)
        return back(request, repair_id, "log", form, saved="entry")

    @router.get("/{repair_id}/entries/{entry_id}/edit")
    def entry_edit(request: Request, repair_id: int, entry_id: int):
        try:
            entry, can_edit = repairs.read_work_entry(workspace, access(request), repair_id, entry_id)
        except RepairError as error:
            return failure(request, error)
        if not can_edit or entry.deleted:
            return failure(request, RepairError(403, "forbidden", "This entry cannot be corrected."))
        credential = access(request).credential
        return render(request, "entry_form.html", repair_id=repair_id, entry=entry,
                      details={"kind": entry.kind, "body": entry.body}, revision=entry.revision,
                      request_key=uuid4().hex, field_errors={}, kinds=sorted(repairs.ENTRY_KINDS),
                      csrf=csrf_token(key, f"entry.correct:{entry_id}", credential))

    @router.post("/{repair_id}/entries/{entry_id}/edit")
    def entry_correct(request: Request, repair_id: int, entry_id: int,
                      form: Annotated[dict[str, str], Depends(entry_form)]):
        credential = access(request).credential
        purpose = f"entry.correct:{entry_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        raw_revision = form.get("revision", "")
        revision = form_int(raw_revision)
        fields = {name: form.get(name, "") for name in ("kind", "body", "reason")}
        try:
            repairs.correct_work_entry(workspace, access(request), repair_id, entry_id,
                                       WorkEntryCorrection(revision=revision, **fields),
                                       key=form.get("key", ""))
        except RepairError as error:
            if error.status in (422, 409):
                return render(request, "entry_form.html", status=error.status, repair_id=repair_id,
                              entry={"id": entry_id}, details=fields, revision=raw_revision,
                              request_key=form.get("key", ""), error=str(error),
                              field_errors=error.fields or {}, kinds=sorted(repairs.ENTRY_KINDS),
                              csrf=csrf_token(key, purpose, credential))
            return failure(request, error)
        return back(request, repair_id, "log", form, saved="entry_corrected")

    @router.get("/{repair_id}/entries/{entry_id}/history")
    def entry_history(request: Request, repair_id: int, entry_id: int):
        try:
            history = repairs.work_entry_history(workspace, access(request), repair_id, entry_id)
        except RepairError as error:
            return failure(request, error)
        return render(request, "entry_history.html", repair_id=repair_id, entry_id=entry_id,
                      history=history)

    @router.get("/{repair_id}/entries/{entry_id}/visibility")
    def entry_visibility(request: Request, repair_id: int, entry_id: int):
        try:
            entry, can_edit = repairs.read_work_entry(workspace, access(request), repair_id, entry_id)
        except RepairError as error:
            return failure(request, error)
        if not can_edit:
            return failure(request, RepairError(403, "forbidden", "This entry cannot change visibility."))
        credential = access(request).credential
        return render(request, "entry_visibility.html", repair_id=repair_id, entry=entry,
                      revision=entry.revision, request_key=uuid4().hex, reason="",
                      csrf=csrf_token(key, f"entry.visibility:{entry_id}", credential))

    @router.post("/{repair_id}/entries/{entry_id}/visibility")
    def entry_visibility_submit(request: Request, repair_id: int, entry_id: int,
                                form: Annotated[dict[str, str], Depends(entry_form)]):
        credential = access(request).credential
        purpose = f"entry.visibility:{entry_id}"
        check_csrf(request, form, csrf_token(key, purpose, credential) if credential else None)
        raw_revision = form.get("revision", "")
        revision = form_int(raw_revision)
        target = form.get("deleted", "")
        reason = form.get("reason", "")
        try:
            if target not in ("0", "1"):
                raise RepairError(422, "validation", "Check the work entry.")
            repairs.change_entry_visibility(workspace, access(request), repair_id, entry_id,
                                            WorkEntryVisibility(revision=revision,
                                                                deleted=target == "1", reason=reason),
                                            key=form.get("key", ""))
        except RepairError as error:
            if error.status in (422, 409):
                try:
                    entry, _ = repairs.read_work_entry(workspace, access(request), repair_id, entry_id)
                except RepairError:
                    raise
                return render(request, "entry_visibility.html", status=error.status,
                              repair_id=repair_id, entry=entry, revision=raw_revision,
                              target=target if target in ("0", "1") else ("0" if entry.deleted else "1"),
                              request_key=form.get("key", ""), reason=reason, error=str(error),
                              conflict=error.status == 409,
                              csrf=csrf_token(key, purpose, credential))
            return failure(request, error)
        return back(request, repair_id, "log", form, saved="entry_visibility")

    return router
