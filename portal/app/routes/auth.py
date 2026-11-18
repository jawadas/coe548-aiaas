"""Sign-up, sign-in and sign-out.

UI CONTRACT

``GET  /signup``  the form, or the "sign-ups are closed" notice
``POST /signup``  fields ``name``, ``email``, ``password``, ``invite_code`` (invite mode
                  only), ``csrf_token``. 303 to ``/overview`` with a welcome flash and
                  the user signed in; 400 with per-field ``errors``; 403 when sign-ups
                  are closed; 429 with ``Retry-After`` when rate limited
``GET  /login``   the form; ``?next=/path`` is carried along in a hidden field
``POST /login``   fields ``email``, ``password``, ``next``, ``csrf_token``. 303 to ``next``
                  (same-site paths only, default ``/overview``); 401 "Invalid email or
                  password"; 403 "This account has been disabled"; 429 with ``Retry-After``
``POST /logout``  field ``csrf_token``. 303 to ``/`` with a flash

Signed-in visitors of ``/login`` and ``/signup`` are redirected to ``/overview``.

Template context (on top of what ``render`` always provides)
  ``auth/login.html``   ``next``, ``email``, ``error`` (message or ``None``), ``retry_after``
  ``auth/signup.html``  ``values`` (``name``, ``email``), ``errors`` (field -> message),
                        ``error``, ``retry_after``; ``settings.signup_open`` and
                        ``settings.invite_required`` say which variant to show
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.datastructures import FormData
from starlette.responses import Response

from app import security, sessions
from app.config import CSRF_COOKIE, Settings
from app.deps import (
    AuthLimitsDep,
    ClientIP,
    CurrentUser,
    PoolDep,
    RequiredUser,
    SettingsDep,
    UsersDep,
    check_rate_limit,
    client_ip,
    get_pool,
    get_settings,
)
from app.templating import redirect, render
from app.users import (
    EMAIL_MAX_LENGTH,
    AccountDisabled,
    InvalidCredentials,
    User,
    UserInputError,
    normalize_email,
    normalize_name,
)

router = APIRouter()

HOME = "/overview"
INVALID_LOGIN = "Invalid email or password"
ACCOUNT_DISABLED = "This account has been disabled"
INVALID_INVITE = "That invite code is not valid."


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _wait_message(what: str, retry_after: int) -> str:
    minutes = math.ceil(retry_after / 60)
    return f"Too many {what}. Try again in {minutes} minute{'' if minutes == 1 else 's'}."


def _retry_headers(retry_after: int | None) -> dict[str, str]:
    return {} if retry_after is None else {"Retry-After": str(retry_after)}


async def _start_session(request: Request, response: Response, user: User) -> None:
    """Create the session, hand its cookie over and retire the anonymous CSRF cookie."""
    settings, pool = get_settings(request), get_pool(request)
    await sessions.purge_expired(pool)
    token, _ = await sessions.create(
        pool,
        user.id,
        lifetime=settings.session_lifetime,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    sessions.set_cookie(response, settings, token)
    response.delete_cookie(CSRF_COOKIE, path="/")


# -- sign-in -------------------------------------------------------------------


def _login_form(
    request: Request,
    *,
    next_path: str,
    email: str = "",
    error: str | None = None,
    status_code: int = 200,
    retry_after: int | None = None,
) -> Response:
    context = {"next": next_path, "email": email, "error": error, "retry_after": retry_after}
    return render(
        request, "auth/login.html", context, status_code, headers=_retry_headers(retry_after)
    )


@router.get("/login")
async def login_page(
    request: Request, user: CurrentUser, next_path: Annotated[str, Query(alias="next")] = ""
) -> Response:
    if user is not None:
        return redirect(request, HOME)
    return _login_form(request, next_path=security.safe_next_path(next_path, ""))


@router.post("/login")
async def login(
    request: Request, user: CurrentUser, users: UsersDep, limits: AuthLimitsDep, ip: ClientIP
) -> Response:
    if user is not None:
        return redirect(request, HOME)
    form = await request.form()
    email = normalize_email(_text(form, "email"))
    next_path = security.safe_next_path(_text(form, "next"), "")
    email_key = email[:EMAIL_MAX_LENGTH]

    retry_after = check_rate_limit(request, limits.login_ip, ip)
    if retry_after is None:
        retry_after = check_rate_limit(request, limits.login_email, email_key)
    if retry_after is not None:
        return _login_form(
            request,
            next_path=next_path,
            email=email,
            error=_wait_message("sign-in attempts", retry_after),
            status_code=429,
            retry_after=retry_after,
        )
    try:
        account = await users.authenticate(email, _text(form, "password"), ip=ip)
    except InvalidCredentials:
        return _login_form(
            request, next_path=next_path, email=email, error=INVALID_LOGIN, status_code=401
        )
    except AccountDisabled:
        return _login_form(
            request, next_path=next_path, email=email, error=ACCOUNT_DISABLED, status_code=403
        )
    # A successful sign-in clears the address's failed attempts (not the IP's).
    limits.login_email.reset(email_key)
    response = redirect(request, next_path or HOME)
    await _start_session(request, response, account)
    return response


# -- sign-up -------------------------------------------------------------------


def _signup_form(
    request: Request,
    *,
    values: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
    retry_after: int | None = None,
) -> Response:
    context = {
        "values": values or {"name": "", "email": ""},
        "errors": errors or {},
        "error": error,
        "retry_after": retry_after,
    }
    return render(
        request, "auth/signup.html", context, status_code, headers=_retry_headers(retry_after)
    )


def _invite_accepted(settings: Settings, submitted: str) -> bool:
    """Open sign-up needs no code; otherwise the configured one must match (fail closed)."""
    if settings.signup_mode == "open":
        return True
    expected = settings.invite_code
    return expected is not None and security.constant_time_equals(
        submitted.strip(), expected.get_secret_value()
    )


@router.get("/signup")
async def signup_page(request: Request, user: CurrentUser) -> Response:
    if user is not None:
        return redirect(request, HOME)
    return _signup_form(request)


@router.post("/signup")
async def signup(
    request: Request,
    user: CurrentUser,
    users: UsersDep,
    settings: SettingsDep,
    limits: AuthLimitsDep,
    ip: ClientIP,
) -> Response:
    if user is not None:
        return redirect(request, HOME)
    if settings.signup_mode == "closed":
        return _signup_form(request, status_code=403)
    form = await request.form()
    values = {
        "name": normalize_name(_text(form, "name")),
        "email": normalize_email(_text(form, "email")),
    }

    retry_after = check_rate_limit(request, limits.signup_ip, ip)
    if retry_after is not None:
        return _signup_form(
            request,
            values=values,
            error=_wait_message("sign-up attempts", retry_after),
            status_code=429,
            retry_after=retry_after,
        )
    # The invite code is checked first: without it, nothing else about the form is revealed.
    if not _invite_accepted(settings, _text(form, "invite_code")):
        return _signup_form(
            request, values=values, errors={"invite_code": INVALID_INVITE}, status_code=400
        )
    try:
        account = await users.create_user(
            email=values["email"], name=values["name"], password=_text(form, "password"), ip=ip
        )
    except UserInputError as exc:
        return _signup_form(request, values=values, errors=exc.errors, status_code=400)
    response = redirect(
        request, HOME, flash=f"Welcome, {account.first_name}! Your account is ready."
    )
    await _start_session(request, response, account)
    return response


# -- sign-out ------------------------------------------------------------------


@router.post("/logout")
async def logout(
    request: Request,
    user: RequiredUser,
    users: UsersDep,
    pool: PoolDep,
    settings: SettingsDep,
    ip: ClientIP,
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await sessions.delete(pool, token)
    await users.record_logout(user, ip=ip)
    response = redirect(request, "/", flash="You have been signed out.", kind="info")
    sessions.clear_cookie(response, settings)
    return response
