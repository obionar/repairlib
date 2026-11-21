"""Repair charge, discount and totals JSON resources (integer cents and milli-units)."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, json_body
from repairlib.contracts import (
    CHARGE_CONSUMPTION_FIELDS, SUMMARY_CONSUMPTION_FIELDS,
    Charge, ChargeInput, ChargePatch, ChargeSummary, ChargeVisibility, DiscountChange,
    IntakeMistake, PartConsumption, PartReturn, PartReturnResult, PartReturnStatus,
)
from repairlib.domain import charges, stock
from repairlib.domain.repairs import RepairAccess

INPUT = TypeAdapter(ChargeInput)
PATCH = TypeAdapter(ChargePatch)
VISIBILITY = TypeAdapter(ChargeVisibility)
DISCOUNT = TypeAdapter(DiscountChange)
CONSUMPTION = TypeAdapter(PartConsumption)
RETURN = TypeAdapter(PartReturn)
INTAKE_MISTAKE = TypeAdapter(IntakeMistake)
CHARGE = TypeAdapter(Charge)
SUMMARY = TypeAdapter(ChargeSummary)


input_json = json_body(INPUT, "charge")
patch_json = json_body(PATCH, "charge correction")
visibility_json = json_body(VISIBILITY, "charge visibility")
discount_json = json_body(DISCOUNT, "discount")
consumption_json = json_body(CONSUMPTION, "part consumption")
return_json = json_body(RETURN, "part return")
mistake_json = json_body(INTAKE_MISTAKE, "intake mistake")


def charge_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/repairs/{repair_id}")

    @router.get("/charges", response_model=ChargeSummary, response_model_exclude=SUMMARY_CONSUMPTION_FIELDS)
    def index(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
              include_consumption: bool = False):
        summary, _ = charges.charge_summary(workspace, access, repair_id)
        return JSONResponse(SUMMARY.dump_python(summary, mode="json")) if include_consumption else summary

    @router.post("/charges", response_model=Charge, status_code=201, openapi_extra=body_schema(INPUT),
                 response_model_exclude=CHARGE_CONSUMPTION_FIELDS)
    def create(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
               details: Annotated[ChargeInput, Depends(input_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return charges.create_charge(workspace, access, repair_id, details, key=key)

    @router.post("/consume-part", response_model=Charge, status_code=201,
                 openapi_extra=body_schema(CONSUMPTION))
    def consume(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                use: Annotated[PartConsumption, Depends(consumption_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return stock.consume_for_repair(workspace, access, repair_id, use, key=key)

    @router.get("/charges/{charge_id}/return-part", response_model=PartReturnStatus)
    def return_status(repair_id: int, charge_id: int, access: Annotated[RepairAccess, Depends(access_dependency)]):
        return stock.part_return_status(workspace, access, repair_id, charge_id)

    @router.post("/charges/{charge_id}/return-part", response_model=PartReturnResult, status_code=201,
                 openapi_extra=body_schema(RETURN))
    def return_part(repair_id: int, charge_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                    change: Annotated[PartReturn, Depends(return_json)],
                    key: Annotated[str, Depends(idempotency_key)]):
        return stock.return_part(workspace, access, repair_id, charge_id, change, key=key)

    @router.post("/charges/{charge_id}/intake-mistake", response_model=PartReturnResult, status_code=201,
                 openapi_extra=body_schema(INTAKE_MISTAKE))
    def intake_mistake(repair_id: int, charge_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                       change: Annotated[IntakeMistake, Depends(mistake_json)],
                       key: Annotated[str, Depends(idempotency_key)]):
        return stock.correct_intake_mistake(workspace, access, repair_id, charge_id, change, key=key)

    @router.get("/charges/{charge_id}", response_model=Charge, response_model_exclude=CHARGE_CONSUMPTION_FIELDS)
    def detail(repair_id: int, charge_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
               include_consumption: bool = False):
        charge, _ = charges.read_charge(workspace, access, repair_id, charge_id)
        return JSONResponse(CHARGE.dump_python(charge, mode="json")) if include_consumption else charge

    @router.patch("/charges/{charge_id}", response_model=Charge, openapi_extra=body_schema(PATCH),
                  response_model_exclude=CHARGE_CONSUMPTION_FIELDS)
    def correct(repair_id: int, charge_id: int,
                access: Annotated[RepairAccess, Depends(access_dependency)],
                patch: Annotated[ChargePatch, Depends(patch_json)],
                key: Annotated[str, Depends(idempotency_key)]):
        return charges.correct_charge(workspace, access, repair_id, charge_id, patch, key=key)

    @router.post("/charges/{charge_id}/visibility", response_model=Charge,
                 openapi_extra=body_schema(VISIBILITY), response_model_exclude=CHARGE_CONSUMPTION_FIELDS)
    def visibility(repair_id: int, charge_id: int,
                   access: Annotated[RepairAccess, Depends(access_dependency)],
                   change: Annotated[ChargeVisibility, Depends(visibility_json)],
                   key: Annotated[str, Depends(idempotency_key)]):
        return charges.change_charge_visibility(workspace, access, repair_id, charge_id, change, key=key)

    @router.put("/discount", response_model=ChargeSummary, openapi_extra=body_schema(DISCOUNT),
                response_model_exclude=SUMMARY_CONSUMPTION_FIELDS)
    def discount(repair_id: int, access: Annotated[RepairAccess, Depends(access_dependency)],
                 change: Annotated[DiscountChange, Depends(discount_json)],
                 key: Annotated[str, Depends(idempotency_key)]):
        return charges.set_discount(workspace, access, repair_id, change, key=key)

    return router
