"""The application: wiring, middleware and error handling.

``create_app()`` builds the FastAPI application. ``uvicorn app.main:app`` gets
one built from the environment the first time ``app`` is looked up, so merely
importing this module (tests, the CLI, tools) never needs a valid environment.

What a request passes through, outermost first:

1. ``SecurityHeadersMiddleware``  security headers and caching policy on every response
2. ``ErrorBoundaryMiddleware``    any unhandled exception becomes the friendly 500
3. ``SessionMiddleware``          the session cookie becomes ``request.state.user`` / ``.session``
4. ``deps.csrf_protect``          app-wide dependency; checks every unsafe request
5. the route's own dependencies and handler

Shared objects live on ``app.state``: ``settings``, ``pool`` (asyncpg),
``gateway`` (``GatewayClient``), ``status`` (``ModelStatusService``), ``users``
(``UserService``), ``auth_limits`` and ``flash_signer``. Routes reach them
through the dependencies in ``app.deps``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlencode

import asyncpg
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import db, security, sessions, users
from app.config import Settings, get_settings
from app.deps import LoginRequired, PortalError, csrf_protect
from app.gateway import GatewayClient, GatewayError, GatewayNotFound, GatewayUnavailable
from app.ratelimit import AuthRateLimits
from app.routes import account, admin, auth, chat, docs, keys, overview, public, usage
from app.status import ModelStatusService
from app.templating import STATIC_DIR, STATIC_URL, error_response, is_api_request, redirect

logger = logging.getLogger(__name__)

ROUTE_MODULES = (public, auth, overview, keys, usage, chat, docs, account, admin)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
IMMUTABLE = "public, max-age=31536000, immutable"

# Probes and the public status poll never need to know who is asking, and must
# keep answering when the database is down.
SESSIONLESS_PATHS = frozenset({"/healthz", "/readyz", "/api/status"})
POOL_CLOSE_SECONDS = 10


def _is_static(path: str) -> bool:
    return path.startswith(f"{STATIC_URL}/")


def _cache_policy(scope: Scope, status_code: int) -> str:
    """Versioned static files are cached for good; nothing else is stored at all.

    ``no-store`` on every page and API response is what keeps one user's data
    out of shared caches and out of the back button after signing out.
    """
    if not _is_static(scope["path"]) or status_code >= 400:
        return "no-store"
    versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return IMMUTABLE if versioned else "no-cache"


class SecurityHeadersMiddleware:
    """Adds the security headers to every response, and ``Cache-Control`` where none is set."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if "cache-control" not in headers:
                    headers["Cache-Control"] = _cache_policy(scope, message["status"])
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ErrorBoundaryMiddleware:
    """Logs unhandled exceptions with their traceback and answers with the generic 500.

    It sits inside the security headers so that this response carries them too
    (an error handler registered with Starlette would run outside every
    middleware). Nothing about the exception reaches the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception("Unhandled error in %s %s", scope["method"], scope["path"])
            if started:
                raise  # too late for an error page; let the server drop the connection
            await error_response(Request(scope, receive), 500)(scope, receive, send)


class SessionMiddleware:
    """Resolves the session cookie before routing.

    Sets ``request.state.user``, ``.session`` and ``.csrf_token`` (all ``None``
    for anonymous visitors), so every page, dependency and error page knows who
    is asking. Looking a session up also slides its expiry.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_sessionless(scope["path"]):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state.update(user=None, session=None, csrf_token=None)
        request = Request(scope, receive)
        token = request.cookies.get(request.app.state.settings.session_cookie_name)
        if token:
            try:
                found = await _load_session(request.app, token)
            except db.UNAVAILABLE_ERRORS:
                logger.exception("Database unavailable while loading a session")
                await error_response(request, 503)(scope, receive, send)
                return
            if found is not None:
                session, user = found
                state.update(user=user, session=session, csrf_token=session.csrf_token)
        await self.app(scope, receive, send)

    @staticmethod
    def _is_sessionless(path: str) -> bool:
        return path in SESSIONLESS_PATHS or _is_static(path)


async def _load_session(app: FastAPI, token: str) -> tuple[sessions.Session, users.User] | None:
    """The live session for a cookie value and its (active) user, or ``None``."""
    async with app.state.pool.acquire() as connection:
        session = await sessions.get(
            connection, token, lifetime=app.state.settings.session_lifetime
        )
        if session is None:
            return None
        user = await users.fetch_user(connection, session.user_id)
    if user is None or not user.is_active:
        return None
    return session, user


# -- error handlers ------------------------------------------------------------


async def _on_database_unavailable(request: Request, exc: Exception) -> Response:
    """The database (or something else a handler was waiting on) cannot be reached."""
    logger.error("Unavailable in %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(request, 503)


async def _on_portal_error(request: Request, exc: PortalError) -> Response:
    return error_response(
        request, exc.status_code, exc.code, exc.message, retry_after=exc.retry_after
    )


async def _on_login_required(request: Request, exc: LoginRequired) -> Response:
    """API calls get a 401; pages go to the login form and come back afterwards."""
    if is_api_request(request):
        return error_response(request, 401)
    settings: Settings = request.app.state.settings
    target = "/login"
    if request.method in ("GET", "HEAD"):
        query = f"?{request.url.query}" if request.url.query else ""
        target += "?" + urlencode({"next": request.url.path + query})
    if settings.session_cookie_name not in request.cookies:
        return redirect(request, target)
    # A session cookie that no longer resolves: say why the login page appeared.
    response = redirect(
        request, target, flash="Your session has expired. Please sign in again.", kind="info"
    )
    sessions.clear_cookie(response, settings)
    return response


async def _on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """Unknown routes, wrong methods and ``HTTPException``s raised by handlers."""
    try:
        standard_phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        standard_phrase = None
    custom = isinstance(exc.detail, str) and exc.detail != standard_phrase
    return error_response(
        request, exc.status_code, message=exc.detail if custom else None, headers=exc.headers
    )


async def _on_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Malformed parameters or bodies. Names the first problem, never echoes the input."""
    message = None
    if is_api_request(request) and exc.errors():
        first = exc.errors()[0]
        location = [str(part) for part in first.get("loc", ())][1:]
        message = ": ".join(filter(None, (".".join(location), str(first.get("msg", "")))))
    return error_response(request, 400, "invalid_request", message or None)


async def _on_gateway_error(request: Request, exc: GatewayError) -> Response:
    """Safety net for gateway failures a handler did not deal with itself."""
    logger.warning(
        "Gateway error in %s %s: %s (status %s)",
        request.method,
        request.url.path,
        exc.code,
        exc.status,
    )
    if isinstance(exc, GatewayUnavailable):
        return error_response(
            request,
            503,
            "gateway_unavailable",
            "The API gateway is not reachable right now. Please try again in a moment.",
        )
    if isinstance(exc, GatewayNotFound):
        return error_response(request, 404)
    return error_response(request, 502)


def _install_error_handlers(app: FastAPI) -> None:
    handlers: dict[type[Exception], Callable[..., Any]] = {
        PortalError: _on_portal_error,
        LoginRequired: _on_login_required,
        StarletteHTTPException: _on_http_exception,
        RequestValidationError: _on_validation_error,
        GatewayError: _on_gateway_error,
        **dict.fromkeys(db.UNAVAILABLE_ERRORS, _on_database_unavailable),
    }
    for error, handler in handlers.items():
        app.add_exception_handler(error, handler)


# -- lifecycle -----------------------------------------------------------------


def _configure_logging() -> None:
    """Send the portal's own log records to stderr (uvicorn only configures its own loggers)."""
    portal_logger = logging.getLogger("app")
    if portal_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    portal_logger.addHandler(handler)
    portal_logger.setLevel(logging.INFO)
    portal_logger.propagate = False


async def _close_pool(pool: asyncpg.Pool) -> None:
    """Close the pool gracefully, but do not let a stuck connection block shutdown."""
    try:
        async with asyncio.timeout(POOL_CLOSE_SECONDS):
            await pool.close()
    except TimeoutError:
        pool.terminate()


def _lifespan(
    settings: Settings, gateway: GatewayClient | None, pool: asyncpg.Pool | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Start-up and shutdown. Resources passed in belong to the caller and are left open."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            state = app.state
            if pool is None:
                state.pool = await db.create_pool(settings.database_url.get_secret_value())
                stack.push_async_callback(_close_pool, state.pool)
            else:
                state.pool = pool
            applied = await db.migrate(state.pool)
            if gateway is None:
                state.gateway = GatewayClient(
                    settings.litellm_base_url,
                    settings.litellm_master_key.get_secret_value(),
                    settings.model,
                )
                stack.push_async_callback(state.gateway.aclose)
            else:
                state.gateway = gateway
            state.status = ModelStatusService(state.gateway)
            stack.push_async_callback(state.status.aclose)
            state.users = users.UserService(state.pool, state.gateway, settings)
            await sessions.purge_expired(state.pool)
            logger.info(
                "Portal started: env=%s, sign-up=%s, migrations applied now=%d",
                settings.env,
                settings.signup_mode,
                len(applied),
            )
            yield

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    gateway: GatewayClient | None = None,
    pool: asyncpg.Pool | None = None,
) -> FastAPI:
    """Build the application.

    ``settings`` default to the environment. ``gateway`` and ``pool`` let tests
    and tools inject a fake gateway or share a connection pool; the application
    then uses them without closing them.
    """
    settings = settings or get_settings()
    _configure_logging()
    app = FastAPI(
        title="AIaaS Portal",
        # /docs is the product's own documentation page, so FastAPI's are off.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(settings, gateway, pool),
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.auth_limits = AuthRateLimits()
    app.state.flash_signer = security.Signer(settings.secret_key.get_secret_value(), "flash")

    app.mount(STATIC_URL, StaticFiles(directory=STATIC_DIR), name="static")
    for module in ROUTE_MODULES:
        app.include_router(module.router)
    _install_error_handlers(app)

    # Added innermost first: the last one added is the first to see a request.
    app.add_middleware(SessionMiddleware)
    app.add_middleware(ErrorBoundaryMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def __getattr__(name: str) -> Any:
    """Build ``app`` on first access (PEP 562), which is when uvicorn asks for it."""
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
