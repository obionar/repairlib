"""Browser error pages: one translated HTML page per refusal, with a back action."""

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

PAGES = {403: "Access denied", 404: "Page not found", 409: "Conflict", 410: "No longer available",
         500: "Something went wrong", 503: "Temporarily unavailable"}
DEFAULT_HEADING = "Request refused"
HIDDEN_FIELDS = {"csrf", "key", "revision", "reviewed", "review_revision", "switch"}
SECRET_WORDS = ("password", "token", "secret", "csrf")
MAX_TYPED_FIELDS = 12
MAX_TYPED_LENGTH = 4000


def is_api(request: Request) -> bool:
    return request.url.path == "/api" or request.url.path.startswith("/api/")


def typed_fields(form: dict[str, str]) -> list[tuple[str, str, str]]:
    """Text a person typed, minus credentials and form plumbing: (id, label, value) rows."""
    rows = []
    for name, value in form.items():
        if (name in HIDDEN_FIELDS or any(word in name.lower() for word in SECRET_WORDS)
                or not value.strip() or len(value) > MAX_TYPED_LENGTH):
            continue
        rows.append((name, name.replace("_", " ").capitalize(), value))
    return rows[:MAX_TYPED_FIELDS]


def back_path(request: Request) -> str:
    """The page the person came from when it is on this origin, otherwise the dashboard."""
    referer = urlsplit(request.headers.get("referer", ""))
    # A leading backslash is a protocol-relative URL to browsers once normalised.
    if ((referer.scheme, referer.netloc) == (request.url.scheme, request.url.netloc)
            and referer.path.startswith("/") and not referer.path.startswith(("//", "/\\"))
            and "\\" not in referer.path and referer.path.isprintable()
            and referer.query.isprintable()):
        return referer.path + (f"?{referer.query}" if referer.query else "")
    return "/"


def error_page(request: Request, status: int, message: str = "", typed=None, request_id: str | None = None):
    return request.app.state.render_error(request, status, message, typed or [], request_id)


def refusal(request: Request, error, redraw):
    """A domain refusal as a browser response: 401 goes to sign-in, statuses with an error page get it,
    anything else is redrawn by the caller as `redraw(request, error)`."""
    if error.status == 401:
        return RedirectResponse(request.url_for("sign_in").path, status_code=303)
    if error.status in PAGES:
        return error_page(request, error.status, str(error))
    return redraw(request, error)
