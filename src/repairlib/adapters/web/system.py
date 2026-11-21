"""Read-only admin system page: no process control, no business rows."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from repairlib.adapters.web.security import browser_access as access, require_runtime
from repairlib.domain import system
from repairlib.domain.users import UserError


def system_routes(workspace: Path, templates) -> APIRouter:
    router = APIRouter(prefix="/system", dependencies=[Depends(require_runtime)])

    @router.get("")
    def system_page(request: Request):
        try:
            facts = system.read_system(workspace, access(request))
        except UserError as error:
            if error.status == 401:
                return RedirectResponse(request.url_for("sign_in").path, status_code=303)
            return templates.TemplateResponse(request, "system.html",
                                              {"facts": None, "error": str(error)},
                                              status_code=error.status)
        return templates.TemplateResponse(request, "system.html", {"facts": facts, "error": ""})

    return router
