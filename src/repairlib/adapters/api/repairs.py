"""Repair and attributed work-entry JSON resources."""

from functools import partial
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import TypeAdapter

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, json_body, page_number as customer_page_number
from repairlib.contracts import (
    DeviceRepair, LabelOptions, LabelPrint, PrintReceipt, Repair, RepairActivityPage, RepairFlagChange, RepairInput, RepairPage,
    RepairPatch, StageChange,
    WorkEntry, WorkEntryCorrection, WorkEntryInput, WorkEntryPage, WorkEntryRevision,
    WorkEntryVisibility,
)
from repairlib.domain.printing import print_p15, read_label_options
from repairlib.domain.repairs import (
    RepairAccess, change_entry_visibility, change_repair_flag, change_stage,
    correct_work_entry, create_repair, create_work_entry, device_history, list_repairs, list_work_entries,
    read_repair, repair_activity, update_repair, work_entry_history,
)

INPUT = TypeAdapter(RepairInput)
LABEL = TypeAdapter(LabelPrint)
PATCH = TypeAdapter(RepairPatch)
ENTRY_INPUT = TypeAdapter(WorkEntryInput)
STAGE = TypeAdapter(StageChange)
FLAG = TypeAdapter(RepairFlagChange)
CORRECTION = TypeAdapter(WorkEntryCorrection)
VISIBILITY = TypeAdapter(WorkEntryVisibility)


label_json = json_body(LABEL, "repair label")
repair_json = json_body(INPUT, "repair")
patch_json = json_body(PATCH, "repair patch")
entry_json = json_body(ENTRY_INPUT, "work entry")
stage_json = json_body(STAGE, "stage change")
flag_json = json_body(FLAG, "repair state change")
correction_json = json_body(CORRECTION, "work-entry correction")
visibility_json = json_body(VISIBILITY, "work-entry visibility")


page_number = partial(customer_page_number, noun="repair")


def repair_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/repairs")

    @router.get("", response_model=RepairPage)
    def index(access: Annotated[RepairAccess, Depends(access_dependency)],
              before: str | None = None, limit: str | None = None, q: str = "",
              stage: str | None = None, picked_up: bool | None = None, archived: bool | None = None,
              technician: str = "", customer_id: int | None = None, queue: str = "",
              paid: bool | None = None):
        page, _ = list_repairs(workspace, access, before=page_number(before),
                               limit=page_number(limit, 25), query=q, stage=stage, picked_up=picked_up,
                               archived=archived, technician=technician, customer_id=customer_id, queue=queue,
                               paid=paid)
        return page

    @router.post("", response_model=Repair, status_code=201, openapi_extra=body_schema(INPUT))
    def create(access: Annotated[RepairAccess, Depends(access_dependency)],
               details: Annotated[RepairInput, Depends(repair_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return create_repair(workspace, access, details, key=key)

    @router.get("/device-history", response_model=list[DeviceRepair])
    def history_by_serial(access: Annotated[RepairAccess, Depends(access_dependency)], serial: str):
        return device_history(workspace, access, serial)

    @router.get("/{repair_id}", response_model=Repair)
    def detail(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)]):
        repair, _, _ = read_repair(workspace, access, repair_id)
        return repair

    @router.get("/{repair_id}/activity", response_model=RepairActivityPage,
                response_model_exclude_defaults=True)
    def activity(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                 before: str | None = None, limit: str | None = None):
        return repair_activity(workspace, access, repair_id, before=page_number(before),
                               limit=page_number(limit, 25))

    @router.get("/{repair_id}/label", response_model=LabelOptions)
    def label_options(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)]):
        return read_label_options(workspace, access, repair_id)

    @router.post("/{repair_id}/label", response_model=PrintReceipt, openapi_extra=body_schema(LABEL))
    def print_label(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                    review: Annotated[LabelPrint, Depends(label_json)],
                    key: Annotated[str, Depends(idempotency_key)]):
        return print_p15(workspace, access, repair_id=repair_id, repair_revision=review.revision,
                         printer_revision=review.printer_revision, key=key)

    @router.patch("/{repair_id}", response_model=Repair, openapi_extra=body_schema(PATCH))
    def edit(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
             changes: Annotated[RepairPatch, Depends(patch_json)],
             key: Annotated[str, Depends(idempotency_key)]):
        return update_repair(workspace, access, repair_id, changes, key=key)

    @router.post("/{repair_id}/stage", response_model=Repair, openapi_extra=body_schema(STAGE))
    def stage(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
              change: Annotated[StageChange, Depends(stage_json)],
              key: Annotated[str, Depends(idempotency_key)]):
        return change_stage(workspace, access, repair_id, change, key=key)

    @router.post("/{repair_id}/payment", response_model=Repair, openapi_extra=body_schema(FLAG))
    def payment(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                change: Annotated[RepairFlagChange, Depends(flag_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return change_repair_flag(workspace, access, repair_id, change, key=key, flag="payment")

    @router.post("/{repair_id}/pickup", response_model=Repair, openapi_extra=body_schema(FLAG))
    def pickup(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
               change: Annotated[RepairFlagChange, Depends(flag_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return change_repair_flag(workspace, access, repair_id, change, key=key, flag="pickup")

    @router.post("/{repair_id}/archive", response_model=Repair, openapi_extra=body_schema(FLAG))
    def archive(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                change: Annotated[RepairFlagChange, Depends(flag_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return change_repair_flag(workspace, access, repair_id, change, key=key, flag="archive")

    @router.get("/{repair_id}/entries", response_model=WorkEntryPage)
    def entries(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                before: str | None = None, limit: str | None = None):
        page, _ = list_work_entries(workspace, access, repair_id, before=page_number(before),
                                    limit=page_number(limit, 25))
        return page

    @router.post("/{repair_id}/entries", response_model=WorkEntry, status_code=201,
                 openapi_extra=body_schema(ENTRY_INPUT))
    def add_entry(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                  details: Annotated[WorkEntryInput, Depends(entry_json)],
                  key: Annotated[str, Depends(idempotency_key)]):
        return create_work_entry(workspace, access, repair_id, details, key=key)

    @router.patch("/{repair_id}/entries/{entry_id}", response_model=WorkEntry,
                  openapi_extra=body_schema(CORRECTION))
    def correct(repair_id: int, entry_id: int,
                access: Annotated[RepairAccess, Depends(access_dependency)],
                correction: Annotated[WorkEntryCorrection, Depends(correction_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return correct_work_entry(workspace, access, repair_id, entry_id, correction, key=key)

    @router.post("/{repair_id}/entries/{entry_id}/visibility", response_model=WorkEntry,
                 openapi_extra=body_schema(VISIBILITY))
    def visibility(repair_id: int, entry_id: int,
                   access: Annotated[RepairAccess, Depends(access_dependency)],
                   change: Annotated[WorkEntryVisibility, Depends(visibility_json)],
                   key: Annotated[str, Depends(idempotency_key)]):
        return change_entry_visibility(workspace, access, repair_id, entry_id, change, key=key)

    @router.get("/{repair_id}/entries/{entry_id}/history", response_model=list[WorkEntryRevision])
    def history(repair_id: int, entry_id: int,
                access: Annotated[RepairAccess, Depends(access_dependency)]):
        return work_entry_history(workspace, access, repair_id, entry_id)

    return router
