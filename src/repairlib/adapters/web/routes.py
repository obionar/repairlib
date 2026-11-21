"""Server-rendered dashboard, account, setup and sign-in routes."""

from datetime import UTC, datetime
import logging
from pathlib import Path
import secrets
from time import time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from repairlib import i18n, money
from repairlib.adapters.web.charges import charge_routes
from repairlib.adapters.web.errors import DEFAULT_HEADING, PAGES, back_path

from repairlib.adapters.web.customers import customer_routes
from repairlib.adapters.web.inventory import inventory_routes
from repairlib.adapters.web.repairs import repair_routes
from repairlib.adapters.web.security import (
    BOOTSTRAP_SECONDS, LOGIN_CSRF_SECONDS, SESSION_COOKIE, Bootstrap, PasswordBudget,
    browser_access, check_csrf, csrf_token, read_form, require_runtime, set_cookie,
)
from repairlib.adapters.web.tokens import token_routes
from repairlib.adapters.web.users import user_routes
from repairlib.adapters.web.settings import settings_routes
from repairlib.adapters.web.system import system_routes
from repairlib.domain import settings as shop_settings
from repairlib.domain.customers import CustomerError
from repairlib.domain.dashboard import read_dashboard
from repairlib.domain.sessions import SESSION_SECONDS, login, logout, principal_for_session
from repairlib.domain.setup import initialize_workspace
from repairlib.lifecycle.workspace import SETUP_STATES, WorkspaceError, workspace_status

ASSETS = Path(__file__).parent
SETUP_COOKIE = "repairlib_setup"
LOGIN_COOKIE = "repairlib_login"
Form = Annotated[dict[str, str], Depends(read_form)]
logger = logging.getLogger(__name__)


def browser_routes(workspace: Path, bootstrap: Bootstrap | None, key: bytearray, app=None) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def browser_principal(request: Request):
        if request.app.state.workspace_status != "initialized":
            return None
        if not hasattr(request.state, "browser_principal"):
            principal = principal_for_session(
                workspace, request.cookies.get(SESSION_COOKIE, ""), now=int(time()),
            )
            request.state.browser_principal = principal
            if principal is not None:
                request.state.verified_actor = principal.shop_id, principal.user_id
        return request.state.browser_principal

    def browser_timezone(request: Request) -> str:
        if not hasattr(request.state, "shop_timezone"):
            timezone = "UTC"
            if browser_principal(request) is not None:
                try:
                    timezone = shop_settings.read_timezone(workspace, browser_access(request))
                except CustomerError:
                    pass
            request.state.shop_timezone = timezone
        return request.state.shop_timezone

    def shell_context(request: Request) -> dict:
        principal = browser_principal(request)
        if principal is None:
            return {}
        section = request.url.path.split("/", 2)[1]
        return {
            "shell_principal": principal,
            "shell_section": "settings" if section == "users" else section,
            "shell_sign_out": csrf_token(key, "logout", request.cookies[SESSION_COOKIE]),
            "shop_timezone": browser_timezone(request),
        }

    templates = Jinja2Templates(directory=ASSETS / "templates", context_processors=[shell_context])
    templates.env.filters["cents"] = money.format_cents
    templates.env.filters["milli"] = money.format_milli

    @pass_context
    def local_time(context, value, *, date_only=False) -> str:
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        timezone = context.get("shop_timezone") or "UTC"
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            state = context["request"].state
            if not getattr(state, "timezone_warning_logged", False):
                logger.warning("Shop timezone %r unavailable; showing stored timestamps. "
                               "Check the IANA zone name and install tzdata on Alpine.", timezone)
                state.timezone_warning_logged = True
            return str(value)
        language = request_language(context["request"])
        return moment.astimezone(zone).strftime(i18n.datetime_format(language, date_only=date_only))

    templates.env.filters["localtime"] = local_time

    @pass_context
    def age(context, value) -> str:
        """Relative age of an ISO timestamp, in the page language."""
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        minutes = max(int((datetime.now(UTC) - moment).total_seconds() // 60), 0)
        say = i18n.translator(request_language(context["request"]))
        if minutes < 1:
            return say("just now")
        if minutes < 60:
            return say("{count} min ago", count=minutes)
        if minutes < 24 * 60:
            return say("{count} h ago", count=minutes // 60)
        return say("{count} d ago", count=minutes // (24 * 60))

    templates.env.filters["age"] = age

    def request_language(request) -> str:
        if not hasattr(request.state, "language"):
            default = ""
            if request.app.state.workspace_status == "initialized":
                default = shop_settings.read_workspace_language(workspace)
            request.state.language = i18n.negotiate(
                request.cookies.get(i18n.COOKIE, ""), request.headers.get("accept-language", ""), default)
        return request.state.language

    @pass_context
    def page_language(context) -> str:
        return request_language(context["request"])

    @pass_context
    def gettext(context, text, /, **values):
        return i18n.translator(request_language(context["request"]))(text, **values)

    @pass_context
    def stage_label(context, stage) -> str:
        """Localized repair stage name; the English title doubles as its message ID."""
        return i18n.translator(request_language(context["request"]))(stage.replace("_", " ").title())

    def render_error(request, status, message="", typed=(), request_id=None):
        return templates.TemplateResponse(request, "error.html", {
            "heading": PAGES.get(status, DEFAULT_HEADING), "message": message, "typed": typed,
            "request_id": request_id, "back": back_path(request),
        }, status_code=status)

    if app is not None:
        app.state.render_error = render_error
    templates.env.globals["_"] = gettext
    templates.env.globals["stage_label"] = stage_label
    templates.env.globals["lang"] = page_language
    templates.env.globals["languages"] = i18n.LANGUAGES
    budget = PasswordBudget()
    router.include_router(token_routes(workspace, templates, key, budget))
    router.include_router(customer_routes(workspace, templates, key))
    router.include_router(inventory_routes(workspace, templates, key))
    router.include_router(charge_routes(workspace, templates, key))
    router.include_router(repair_routes(workspace, templates, key))
    router.include_router(user_routes(workspace, templates, key))
    router.include_router(settings_routes(workspace, templates, key))
    router.include_router(system_routes(workspace, templates))

    def page(request, title, *, status=200, headers=None, **context):
        return templates.TemplateResponse(
            request, "account.html", {"heading": title, **context}, status_code=status,
            headers=headers,
        )

    @router.get("/language")
    def choose_language(lang: str = "", next: str = "/"):
        target = next if next.startswith("/") and not next.startswith(("//", "/\\")) and next.isprintable() else "/"
        response = RedirectResponse(target, status_code=303)
        if lang in i18n.LANGUAGES:
            response.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, httponly=True, samesite="lax")
        else:
            response.delete_cookie(i18n.COOKIE)
        return response

    def setup_available(request: Request) -> bool:
        return (
            request.app.state.workspace_status in SETUP_STATES
            and workspace_status(workspace) in SETUP_STATES
        )

    def active_setup(request: Request) -> Bootstrap:
        if not setup_available(request):
            raise HTTPException(404, "Workspace setup is closed.")
        if bootstrap is None:
            raise HTTPException(403, "Open the setup link from the development server terminal.")
        return bootstrap

    def setup_csrf(request: Request, setup: Bootstrap) -> str:
        grant = request.cookies.get(SETUP_COOKIE, "")
        if not setup.allows(grant):
            raise HTTPException(403, "Setup link or session expired. Restart browser setup.")
        return csrf_token(key, "setup", grant)

    @router.get("/")
    def home(request: Request):
        status = request.app.state.workspace_status
        if status == "initialized":
            try:
                dashboard = read_dashboard(workspace, browser_access(request))
            except CustomerError as error:
                if error.status == 401:
                    return RedirectResponse(request.url_for("sign_in").path, status_code=303)
                raise
            return templates.TemplateResponse(
                request, "dashboard.html",
                {"dashboard": dashboard})
        if status in SETUP_STATES:
            if workspace_status(workspace) == "initialized":
                return page(request, "Workspace created",
                            message="Restart the development server to sign in.")
            return page(request, "Set up workspace",
                        message="Open the link printed by dev --browser-setup, or run repairlib setup.")
        return page(request, "Workspace unavailable", status=503,
                    message="Run repairlib doctor for details.")

    @router.get("/account", dependencies=[Depends(require_runtime)])
    def account(request: Request):
        principal = browser_principal(request)
        if principal is None:
            return RedirectResponse(request.url_for("sign_in").path, status_code=303)
        return page(request, "Account", username=principal.username, role=principal.role,
                    account_page=True)

    @router.get("/setup/exchange")
    def exchange(request: Request):
        setup = active_setup(request)
        tokens = request.query_params.getlist("token")
        grant = setup.exchange(tokens[0]) if len(tokens) == 1 else None
        if grant is None:
            raise HTTPException(403, "Setup link is invalid, expired or already used.")
        response = RedirectResponse("/setup", status_code=303)
        set_cookie(request, response, SETUP_COOKIE, grant, path="/setup",
                   max_age=BOOTSTRAP_SECONDS)
        return response

    @router.get("/setup")
    def setup_page(request: Request):
        return page(request, "Set up workspace", action="setup",
                    csrf=setup_csrf(request, active_setup(request)),
                    fields={"timezone": "UTC"})

    @router.post("/setup")
    def setup_submit(request: Request, form: Form):
        setup = active_setup(request)
        token = setup_csrf(request, setup)
        check_csrf(request, form, token)
        fields = {name: form.get(name, "") for name in ("shop", "timezone", "username")}
        with budget.attempt() as wait:
            if wait:
                return page(request, "Set up workspace", action="setup", csrf=token, fields=fields,
                            status=429, headers={"Retry-After": str(wait)},
                            error="Too many attempts. Wait a moment and try again.")
            try:
                initialize_workspace(workspace, **fields, password=form.get("password", ""))
            except ValueError as error:
                return page(request, "Set up workspace", action="setup", csrf=token, fields=fields,
                            status=422, error=str(error))
            except WorkspaceError:
                if not setup_available(request):
                    raise HTTPException(404, "Workspace setup is closed.") from None
                return page(request, "Set up workspace", action="setup", csrf=token, fields=fields,
                            status=409, error="Workspace state changed. Run repairlib doctor or try again.")
            except OSError:
                return page(request, "Set up workspace", action="setup", csrf=token, fields=fields,
                            status=503, error="Could not publish workspace state. Run repairlib doctor.")
        setup.close()
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SETUP_COOKIE, path="/setup")
        return response

    @router.get("/login")
    def sign_in(request: Request):
        require_runtime(request)
        if browser_principal(request) is not None:
            return RedirectResponse(request.url_for("home").path, status_code=303)
        nonce = request.cookies.get(LOGIN_COOKIE) or secrets.token_urlsafe(32)
        response = page(request, "Sign in", action="login",
                        csrf=csrf_token(key, "login", nonce), fields={},
                        switching=request.query_params.get("switch") == "1")
        set_cookie(request, response, LOGIN_COOKIE, nonce, path="/login", max_age=LOGIN_CSRF_SECONDS)
        return response

    @router.post("/login")
    def sign_in_submit(request: Request, form: Form):
        require_runtime(request)
        nonce = request.cookies.get(LOGIN_COOKIE)
        token = csrf_token(key, "login", nonce) if nonce else None
        check_csrf(request, form, token)
        if browser_principal(request) is not None:
            return RedirectResponse(request.url_for("home").path, status_code=303)
        username = form.get("username", "")
        session_only = form.get("session_only") == "1"
        with budget.attempt() as wait:
            if wait:
                return page(request, "Sign in", action="login", csrf=token, fields={"username": username},
                            session_only=session_only, status=429, headers={"Retry-After": str(wait)},
                            error="Too many attempts. Wait a moment and try again.")
            session = login(workspace, username, form.get("password", ""), now=int(time()))
        if session is None:
            return page(request, "Sign in", action="login", csrf=token, fields={"username": username},
                        session_only=session_only, status=401, error="Username or password is incorrect.")
        response = RedirectResponse(request.url_for("home").path, status_code=303)
        set_cookie(request, response, SESSION_COOKIE, session, path="/",
                   max_age=None if session_only else SESSION_SECONDS)
        response.delete_cookie(LOGIN_COOKIE, path="/login")
        return response

    @router.post("/logout")
    def sign_out(request: Request, form: Form):
        require_runtime(request)
        session = request.cookies.get(SESSION_COOKIE)
        token = csrf_token(key, "logout", session) if session else None
        check_csrf(request, form, token)
        if session:
            logout(workspace, session, now=int(time()))
        target = request.url_for("sign_in").path
        if form.get("switch") == "1":
            target += "?switch=1"
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router
