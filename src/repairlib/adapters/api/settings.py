"""Shop settings JSON resource; no deployment configuration is exposed."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import TypeAdapter

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, json_body
from repairlib.contracts import (
    PrintReceipt, PrinterChange, PrinterSettings, PrinterTest, SettingsPatch, ShopSettings,
)
from repairlib.domain.printing import configure_printer, print_p15, read_printer
from repairlib.domain.settings import SettingsAccess, read_settings, update_settings

PATCH = TypeAdapter(SettingsPatch)
PRINTER_CHANGE = TypeAdapter(PrinterChange)
PRINTER_TEST = TypeAdapter(PrinterTest)


settings_json = json_body(PATCH, "settings patch")
printer_json = json_body(PRINTER_CHANGE, "printer configuration")
printer_test_json = json_body(PRINTER_TEST, "test label")


def settings_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/settings")

    @router.get("", response_model=ShopSettings)
    def detail(access: Annotated[SettingsAccess, Depends(access_dependency)]):
        return read_settings(workspace, access)

    @router.patch("", response_model=ShopSettings, openapi_extra=body_schema(PATCH))
    def edit(access: Annotated[SettingsAccess, Depends(access_dependency)],
             changes: Annotated[SettingsPatch, Depends(settings_json)],
             key: Annotated[str, Depends(idempotency_key)]):
        return update_settings(workspace, access, changes, key=key)

    @router.get("/printer", response_model=PrinterSettings)
    def printer_detail(access: Annotated[SettingsAccess, Depends(access_dependency)]):
        return read_printer(workspace, access)

    @router.patch("/printer", response_model=PrinterSettings, openapi_extra=body_schema(PRINTER_CHANGE))
    def printer_edit(access: Annotated[SettingsAccess, Depends(access_dependency)],
                     change: Annotated[PrinterChange, Depends(printer_json)],
                     key: Annotated[str, Depends(idempotency_key)]):
        return configure_printer(workspace, access, change, key=key)

    @router.post("/printer/test", response_model=PrintReceipt, openapi_extra=body_schema(PRINTER_TEST))
    def printer_test(access: Annotated[SettingsAccess, Depends(access_dependency)],
                     review: Annotated[PrinterTest, Depends(printer_test_json)],
                     key: Annotated[str, Depends(idempotency_key)]):
        return print_p15(workspace, access, printer_revision=review.revision, key=key)

    return router
