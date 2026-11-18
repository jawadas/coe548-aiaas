"""FastAPI dependencies, and the errors that routes raise.

How a route gets what it needs::

    @router.post("/api/things")
    async def create_thing(user: RequiredUser, pool: PoolDep, gateway: GatewayDep) -> ...:

``CurrentUser``    the signed-in ``User`` or ``None`` (public pages)
``RequiredUser``   the signed-in user; anonymous visitors are redirected to
                   ``/login?next=...`` on pages and get a 401 JSON error under ``/api/``
``AdminUser``      the same, plus 403 for anyone who is not an admin
``PoolDep``        the asyncpg pool (use this in handlers that stream a response)
``ConnDep``        one connection held for the whole request (for transactions)
``GatewayDep``     the ``GatewayClient``
``StatusDep``      the ``ModelStatusService``
``UsersDep``       the ``UserService``
``SettingsDep``, ``ClientIP``, ``AuthLimitsDep``

The middleware has already resolved the session before any of these run, so
``request.state.user``, ``request.state.session`` and ``request.state.csrf_token``
are always set (``None`` for anonymous visitors).

CSRF protection needs nothing from a route: ``csrf_protect`` is installed on
the whole application and checks every POST, PUT, PATCH and DELETE.

To fail a request, raise ``PortalError(status, code, message)``. Page routes
turn it into the HTML error page, ``/api/`` routes into
``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, Request

from app import security
from app.config import CSRF_COOKIE, Settings, origin_of
from app.gateway import GatewayClient
from app.ratelimit import AuthRateLimits, SlidingWindowLimiter
from app.sessions import Session
from app.status import ModelStatusService
from app.users import User, UserService

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class PortalError(Exception):
    """A request cannot be served; carries what the client should be told.

    ``code`` is a stable snake_case identifier (``not_found``, ``name_taken``),
    ``message`` a sentence that is safe to show to the user. ``retry_after``
    (seconds) becomes a ``Retry-After`` header.
    """

    def __init__(
        self, status_code: int, code: str, message: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


class LoginRequired(Exception):
    """The route needs a signed-in user and the visitor is anonymous."""


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


async def get_connection(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """One pooled connection for the duration of the request.

    Do not use it in a handler that streams its response: the connection would
    stay checked out for as long as the stream lasts. Use the pool there.
    """
    async with request.app.state.pool.acquire() as connection:
        yield connection


def get_gateway(request: Request) -> GatewayClient:
    return request.app.state.gateway


def get_status_service(request: Request) -> ModelStatusService:
    return request.app.state.status


def get_users(request: Request) -> UserService:
    return request.app.state.users


def get_auth_limits(request: Request) -> AuthRateLimits:
    return request.app.state.auth_limits


def current_user(request: Request) -> User | None:
    """The signed-in user, or ``None`` for an anonymous visitor."""
    return getattr(request.state, "user", None)


def current_session(request: Request) -> Session | None:
    """The session behind this request, or ``None`` for an anonymous visitor."""
    return getattr(request.state, "session", None)


def require_user(request: Request) -> User:
    """The signed-in user. Anonymous visitors get the login page (or a 401 under ``/api/``)."""
    user = current_user(request)
    if user is None:
        raise LoginRequired
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    """The signed-in user, who must be an admin."""
    if not user.is_admin:
        raise PortalError(403, "forbidden", "This area is for administrators only.")
    return user


def client_ip(request: Request) -> str:
    """The visitor's IP address, for rate limiting and the audit log.

    Taken from the first entry of ``X-Forwarded-For``, which is trustworthy
    because Caddy, the only way in, replaces whatever the client sent. Without
    the header (local development, tests) it is the socket's peer address.
    """
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    peer = request.client.host if request.client else ""
    for candidate in (forwarded, peer):
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return "unknown"


def check_rate_limit(request: Request, limiter: SlidingWindowLimiter, key: str) -> int | None:
    """Count one attempt against ``key``. Returns the seconds to wait if it is over the limit.

    ``None`` means the attempt may proceed. Always ``None`` when
    PORTAL_AUTH_RATE_LIMIT is off.
    """
    if not get_settings(request).auth_rate_limit:
        return None
    result = limiter.hit(key)
    return None if result.allowed else result.retry_after


def enforce_rate_limit(
    request: Request,
    limiter: SlidingWindowLimiter,
    key: str,
    message: str = "Too many attempts. Please wait a little and try again.",
) -> None:
    """Like ``check_rate_limit``, but raises the 429 ``PortalError`` itself."""
    retry_after = check_rate_limit(request, limiter, key)
    if retry_after is not None:
        raise PortalError(429, "rate_limited", message, retry_after=retry_after)


def _origin_allowed(request: Request, settings: Settings) -> bool:
    """True unless the browser says the request was sent by another site.

    ``Origin`` is checked when present, otherwise ``Referer``; a request with
    neither (curl, some privacy tools) is left to the token check. Outside
    production the request's own host is accepted too, so the portal works on
    whatever port it is started on.
    """
    origin = request.headers.get("origin")
    source = origin if origin is not None else request.headers.get("referer")
    if source is None:
        return True
    allowed = {settings.public_origin}
    if not settings.is_production:
        host = request.headers.get("host", "")
        allowed |= {origin_of(f"http://{host}"), origin_of(f"https://{host}")}
    return origin_of(source) in allowed


def _requires_login(route: Any) -> bool:
    """True if the matched route depends on ``require_user`` or ``require_admin``."""
    dependant = getattr(route, "dependant", None)
    pending = [dependant] if dependant is not None else []
    while pending:
        node = pending.pop()
        if node.call in (require_user, require_admin):
            return True
        pending.extend(node.dependencies)
    return False


async def _submitted_token(request: Request) -> str | None:
    """The CSRF token the client sent: the ``X-CSRF-Token`` header or the form field."""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    if request.headers.get("content-type", "").startswith(_FORM_CONTENT_TYPES):
        value = (await request.form()).get(CSRF_FIELD)
        return value if isinstance(value, str) else None
    return None


async def csrf_protect(request: Request) -> None:
    """Reject cross-site requests on unsafe methods. Applied to every route.

    Two checks, both of which must pass:

    1. ``Origin`` / ``Referer``, when the browser sends one, must be this site.
    2. The request must carry a token only this site's pages know, in the
       ``X-CSRF-Token`` header or the ``csrf_token`` form field. For a
       signed-in user that is the session's token; for an anonymous visitor
       (the login and sign-up forms) it is the value of the ``aiaas_csrf``
       cookie, which ``templating.render`` sets and writes into the page.

    An anonymous request to a route that requires a user skips the token
    check: it carries no authority to abuse, and ``require_user`` is about to
    answer it with the login redirect or a 401, which is the answer an expired
    session should get.
    """
    if request.method not in UNSAFE_METHODS:
        return
    if not _origin_allowed(request, get_settings(request)):
        raise PortalError(403, "csrf_failed", "This request came from another site.")
    session = current_session(request)
    if session is None and _requires_login(request.scope.get("route")):
        return
    expected = session.csrf_token if session else request.cookies.get(CSRF_COOKIE)
    if not expected or not security.constant_time_equals(await _submitted_token(request), expected):
        raise PortalError(
            403,
            "csrf_failed",
            "This form has expired. Go back, reload the page and try again.",
        )


SettingsDep = Annotated[Settings, Depends(get_settings)]
PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
ConnDep = Annotated[asyncpg.Connection, Depends(get_connection)]
GatewayDep = Annotated[GatewayClient, Depends(get_gateway)]
StatusDep = Annotated[ModelStatusService, Depends(get_status_service)]
UsersDep = Annotated[UserService, Depends(get_users)]
AuthLimitsDep = Annotated[AuthRateLimits, Depends(get_auth_limits)]
CurrentUser = Annotated[User | None, Depends(current_user)]
RequiredUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
ClientIP = Annotated[str, Depends(client_ip)]
