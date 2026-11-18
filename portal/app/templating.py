"""HTML rendering: the Jinja environment, ``render``, flash messages and error responses.

A page handler ends with::

    return render(request, "keys.html", {"keys": keys})

``render`` adds what every template may use:

``user``         the signed-in ``User`` or ``None``
``csrf_token``   put it in ``<input type="hidden" name="csrf_token">`` of every POST
                 form; scripts read it from ``<meta name="csrf-token">`` and send it
                 as the ``X-CSRF-Token`` header
``settings``     ``public_url``, ``model``, ``env``, ``signup_open``, ``invite_required``
``nav_active``   ``overview`` | ``chat`` | ``keys`` | ``usage`` | ``docs`` | ``account`` |
                 ``admin`` | ``None``, from the first segment of the path
``flash``        the one-time messages for this page: a list of ``Flash(kind, message)``
``request``      the Starlette request

plus the global ``static_url('css/plain.css')`` and the filters ``compact``
(46.2k), ``number`` (46,200), ``relative_time`` (2 min ago), ``utc_date``
(18 Nov 2026), ``utc_datetime`` (18 Nov 2026, 12:34 UTC) and ``iso``.

The Content-Security-Policy forbids inline scripts and styles, so templates
must not contain ``<script>`` with code, ``<style>`` or ``style=""``. Pass data
to scripts with ``<script type="application/json">{{ data|tojson }}</script>``
or ``data-*`` attributes.

After a form POST, answer with ``redirect(request, "/somewhere", flash="Saved.")``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app import security
from app.config import CSRF_COOKIE, FLASH_COOKIE, Settings

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
STATIC_URL = "/static"

FLASH_MAX_AGE = 120  # seconds: long enough to survive the redirect it is meant for
NAV_SECTIONS = frozenset({"overview", "chat", "keys", "usage", "docs", "account", "admin"})
_COMPACT_UNITS = ("", "k", "M", "B", "T")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")  # what security.new_token() produces

FlashKind = Literal["success", "info", "error"]


@dataclass(frozen=True, slots=True)
class Flash:
    """A one-time message shown on the next page the user sees."""

    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class ErrorText:
    """Default wording for an HTTP error status."""

    code: str
    title: str
    message: str


ERROR_TEXTS: dict[int, ErrorText] = {
    400: ErrorText("bad_request", "Bad request", "The request could not be understood."),
    401: ErrorText("not_authenticated", "Sign in required", "Sign in to continue."),
    403: ErrorText("forbidden", "Access denied", "You do not have access to this page."),
    404: ErrorText("not_found", "Page not found", "There is nothing at this address."),
    405: ErrorText("method_not_allowed", "Not allowed", "This address does not accept that."),
    409: ErrorText("conflict", "Conflict", "The request conflicts with the current state."),
    413: ErrorText("payload_too_large", "Too large", "The request is too large."),
    422: ErrorText("invalid_request", "Invalid request", "The request is not valid."),
    429: ErrorText(
        "rate_limited", "Too many requests", "You are going too fast. Wait a moment and try again."
    ),
    500: ErrorText(
        "internal_error",
        "Something went wrong",
        "An unexpected error occurred on our side. Please try again.",
    ),
    502: ErrorText(
        "gateway_error", "Gateway error", "The API gateway could not process the request."
    ),
    503: ErrorText(
        "service_unavailable",
        "Temporarily unavailable",
        "The service is temporarily unavailable. Please try again in a moment.",
    ),
}


# -- filters ---------------------------------------------------------------


def compact(value: float | None) -> str:
    """Short number for tiles and tables: 950, 1.2k, 46.2k, 1.2M, 3B.

    Below 1000 the number is rounded to an integer. Above, it is shown with
    one decimal (dropped when it is ``.0``) and the unit k, M, B or T.
    """
    number = float(value or 0)
    unit = 0
    while abs(number) >= 999.95 and unit < len(_COMPACT_UNITS) - 1:
        number /= 1000
        unit += 1
    if unit == 0:
        return str(round(number))
    return f"{number:.1f}".removesuffix(".0") + _COMPACT_UNITS[unit]


def number(value: float | None) -> str:
    """Whole number with thousands separators: 46,200."""
    return f"{round(value or 0):,}"


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def utc_date(value: dt.datetime | dt.date | None) -> str:
    """``18 Nov 2026``. Datetimes are converted to UTC first."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        value = _as_utc(value)
    return f"{value.day} {value:%b %Y}"


def utc_datetime(value: dt.datetime | None) -> str:
    """``18 Nov 2026, 12:34 UTC``."""
    if value is None:
        return ""
    value = _as_utc(value)
    return f"{value.day} {value:%b %Y, %H:%M} UTC"


def iso(value: dt.datetime | None) -> str:
    """ISO 8601 in UTC with a ``Z``, for ``<time datetime="...">`` and for scripts."""
    if value is None:
        return ""
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def relative_time(value: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """``just now``, ``2 min ago``, ``3 hr ago``, ``yesterday``, ``5 days ago``, then the date.

    ``None`` reads ``never``.
    """
    if value is None:
        return "never"
    seconds = ((now or dt.datetime.now(dt.UTC)) - _as_utc(value)).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return utc_date(value)


# -- static files ------------------------------------------------------------


@lru_cache(maxsize=1024)
def _content_hash(path: str, modified_ns: int, size: int) -> str:
    """Short hash of a static file. The extra arguments make edits invalidate the cache."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """URL of a file under ``app/static`` with ``?v=<content hash>`` appended.

    The hash changes whenever the file does, which lets the server tell
    browsers to cache versioned files for a year.
    """
    relative = path.lstrip("/")
    file = STATIC_DIR / relative
    try:
        stat = file.stat()
    except OSError:
        logger.warning("static_url: no such file: %s", relative)
        return f"{STATIC_URL}/{relative}"
    return f"{STATIC_URL}/{relative}?v={_content_hash(str(file), stat.st_mtime_ns, stat.st_size)}"


# -- environment ---------------------------------------------------------------

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals["static_url"] = static_url
env.filters.update(
    compact=compact,
    number=number,
    relative_time=relative_time,
    utc_date=utc_date,
    utc_datetime=utc_datetime,
    iso=iso,
)


# -- flash messages --------------------------------------------------------------


def set_flash(
    request: Request, response: Response, message: str, kind: FlashKind = "success"
) -> None:
    """Attach a one-time message to ``response``; the next rendered page shows it.

    It travels in a short-lived, HMAC-signed cookie, so it survives exactly the
    redirect it was made for and cannot be forged or altered by the browser.
    """
    signer: security.Signer = request.app.state.flash_signer
    settings: Settings = request.app.state.settings
    payload = json.dumps([{"kind": kind, "message": message}])
    response.set_cookie(
        FLASH_COOKIE,
        signer.sign(payload),
        max_age=FLASH_MAX_AGE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _read_flash(request: Request) -> list[Flash]:
    """The messages waiting in the flash cookie, if it is genuine and fresh."""
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return []
    signer: security.Signer = request.app.state.flash_signer
    payload = signer.unsign(raw, FLASH_MAX_AGE)
    if payload is None:
        return []
    try:
        items = json.loads(payload)
        return [Flash(str(item["kind"]), str(item["message"])) for item in items]
    except (ValueError, TypeError, KeyError):
        return []


def redirect(
    request: Request, url: str, *, flash: str | None = None, kind: FlashKind = "success"
) -> RedirectResponse:
    """A 303 redirect (the right answer to a form POST), optionally with a flash message."""
    response = RedirectResponse(url, status_code=303)
    if flash:
        set_flash(request, response, flash, kind)
    return response


# -- rendering ---------------------------------------------------------------------


def _csrf_token(request: Request) -> tuple[str, bool]:
    """The token for this page's forms, and whether the anonymous cookie must be (re)issued.

    Signed-in users get their session's token. Anonymous visitors get the value
    of the ``aiaas_csrf`` cookie, created here on their first page.
    """
    session = getattr(request.state, "session", None)
    if session is not None:
        return session.csrf_token, False
    existing = request.cookies.get(CSRF_COOKIE, "")
    if _TOKEN_PATTERN.fullmatch(existing):
        return existing, False
    return security.new_token(), True


def _nav_active(path: str) -> str | None:
    section = path.strip("/").split("/", 1)[0]
    return section if section in NAV_SECTIONS else None


def site_settings(settings: Settings) -> dict[str, Any]:
    """The few settings templates may show. Never add secrets here."""
    return {
        "public_url": settings.public_url,
        "model": settings.model,
        "env": settings.env,
        "signup_open": settings.signup_mode != "closed",
        "invite_required": settings.signup_mode == "invite",
    }


def render(
    request: Request,
    template: str,
    context: Mapping[str, Any] | None = None,
    status_code: int = 200,
    *,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """Render a template to an HTML response. See the module docstring for the context."""
    settings: Settings = request.app.state.settings
    csrf_token, issue_csrf_cookie = _csrf_token(request)
    page: dict[str, Any] = {
        "request": request,
        "user": getattr(request.state, "user", None),
        "csrf_token": csrf_token,
        "settings": site_settings(settings),
        "nav_active": _nav_active(request.url.path),
        "flash": _read_flash(request),
        **(context or {}),
    }
    html = env.get_template(template).render(page)
    response = HTMLResponse(html, status_code=status_code, headers=headers)
    if issue_csrf_cookie:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
    if FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


# -- errors ----------------------------------------------------------------------


def is_api_request(request: Request) -> bool:
    """``/api/`` routes speak JSON; everything else is a page."""
    return request.url.path.startswith("/api/")


def error_response(
    request: Request,
    status_code: int,
    code: str | None = None,
    message: str | None = None,
    *,
    retry_after: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """The one place that turns an error into a response.

    ``/api/`` requests get ``{"error": {"code", "message"}}``; pages get
    ``errors/error.html`` (context: ``status_code``, ``code``, ``title``,
    ``message``, ``retry_after``). ``code`` and ``message`` default to the
    standard wording for the status. The page must not need a database or a
    gateway: it is also what users see when those are down.
    """
    text = ERROR_TEXTS.get(status_code) or ERROR_TEXTS[500 if status_code >= 500 else 400]
    code = code or text.code
    message = message or text.message
    all_headers = dict(headers or {})
    if retry_after is not None:
        all_headers["Retry-After"] = str(retry_after)
    if is_api_request(request):
        return JSONResponse(
            {"error": {"code": code, "message": message}}, status_code, headers=all_headers
        )
    context = {
        "status_code": status_code,
        "code": code,
        "title": text.title,
        "message": message,
        "retry_after": retry_after,
    }
    try:
        return render(request, "errors/error.html", context, status_code, headers=all_headers)
    except Exception:
        logger.exception("Could not render the error page for status %d", status_code)
        return PlainTextResponse(
            f"{status_code} {text.title}\n\n{message}\n", status_code, headers=all_headers
        )
