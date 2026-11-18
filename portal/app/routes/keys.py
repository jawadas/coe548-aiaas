"""API keys: list them, create one (its secret is shown once), revoke one.

Plain forms, no JavaScript needed:
``GET  /keys``                 the page
``POST /keys``                 field ``name``; answers with the page showing the new secret once
``POST /keys/{token}/revoke``  303 back to ``/keys`` with a flash message
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, Request
from starlette.responses import Response

from app import db
from app.deps import ClientIP, GatewayDep, PoolDep, RequiredUser, SettingsDep, UsersDep
from app.gateway import GatewayClient, GatewayError, GatewayUnavailable, KeyInfo, NewKey
from app.routes.overview import api_keys
from app.services import usage as usage_service
from app.templating import error_response, redirect, render
from app.users import User

logger = logging.getLogger(__name__)
router = APIRouter()

NAME_MAX_LENGTH = 40
USAGE_DAYS = 30
_TOKEN = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def name_error(name: str, existing: list[KeyInfo]) -> str | None:
    if not name:
        return "Give the key a name so you can recognise it later."
    if len(name) > NAME_MAX_LENGTH:
        return f"Use at most {NAME_MAX_LENGTH} characters."
    if _CONTROL.search(name):
        return "The name contains characters that cannot be used."
    if any(key.name.casefold() == name.casefold() for key in existing):
        return "You already have a key with this name."
    return None


async def _page(
    request: Request,
    user: User,
    gateway: GatewayClient,
    *,
    max_keys: int,
    settings_limits: tuple[int, int],
    new_key: NewKey | None = None,
    error: str | None = None,
    name: str = "",
    status_code: int = 200,
) -> Response:
    gateway_down = False
    keys: list[KeyInfo] = []
    requests_by_key: dict[str, int] = {}
    try:
        keys = api_keys(await gateway.list_keys(user.gateway_user_id))
        start, end = usage_service.date_range(USAGE_DAYS)
        for day in await gateway.daily_usage(user.gateway_user_id, start, end):
            for entry in day.by_key:
                requests_by_key[entry.token] = requests_by_key.get(entry.token, 0) + entry.requests
    except GatewayError as exc:
        logger.warning("Keys page without gateway data: %s", exc.code)
        gateway_down = True
    context = {
        "keys": keys,
        "requests_by_key": requests_by_key,
        "max_keys": max_keys,
        "can_create": len(keys) < max_keys and not gateway_down,
        "key_rpm_limit": settings_limits[0],
        "key_tpm_limit": settings_limits[1],
        "new_key": new_key,
        "error": error,
        "name": name,
        "gateway_down": gateway_down,
        "usage_days": USAGE_DAYS,
    }
    return render(request, "keys.html", context, status_code)


@router.get("/keys")
async def keys_page(
    request: Request,
    user: RequiredUser,
    gateway: GatewayDep,
    users: UsersDep,
    settings: SettingsDep,
) -> Response:
    return await _page(
        request,
        user,
        gateway,
        max_keys=users.limits_for(user).max_keys,
        settings_limits=(settings.key_rpm_limit, settings.key_tpm_limit),
    )


@router.post("/keys")
async def create_key(
    request: Request,
    user: RequiredUser,
    gateway: GatewayDep,
    users: UsersDep,
    settings: SettingsDep,
    pool: PoolDep,
    ip: ClientIP,
    name: str = Form(default=""),
) -> Response:
    name = " ".join(name.split())
    max_keys = users.limits_for(user).max_keys
    limits = (settings.key_rpm_limit, settings.key_tpm_limit)

    async def page(**kwargs: object) -> Response:
        return await _page(
            request, user, gateway, max_keys=max_keys, settings_limits=limits, **kwargs
        )

    try:
        await users.ensure_provisioned(user)
        existing = api_keys(await gateway.list_keys(user.gateway_user_id))
        if len(existing) >= max_keys:
            message = f"You already have {max_keys} keys. Revoke one to create another."
            return await page(error=message, name=name, status_code=409)
        if problem := name_error(name, existing):
            return await page(error=problem, name=name, status_code=400)
        new_key = await gateway.create_key(user.gateway_user_id, name, "api", *limits)
    except GatewayUnavailable:
        return await page(name=name, status_code=503)
    await db.audit(
        pool,
        "key.create",
        actor_user_id=user.id,
        target=new_key.info.masked,
        detail={"name": name},
        ip=ip,
    )
    return await page(new_key=new_key)


@router.post("/keys/{token}/revoke")
async def revoke_key(
    request: Request,
    token: str,
    user: RequiredUser,
    gateway: GatewayDep,
    pool: PoolDep,
    ip: ClientIP,
) -> Response:
    if not _TOKEN.fullmatch(token):
        return error_response(request, 404)
    try:
        own = {key.token: key for key in api_keys(await gateway.list_keys(user.gateway_user_id))}
        key = own.get(token)
        if key is None:  # not this user's key, or not a key they may revoke
            return error_response(request, 404)
        await gateway.delete_key(token)
    except GatewayUnavailable:
        message = "The gateway is not reachable. The key was not revoked."
        return redirect(request, "/keys", flash=message, kind="error")
    await db.audit(
        pool,
        "key.revoke",
        actor_user_id=user.id,
        target=key.masked,
        detail={"name": key.name},
        ip=ip,
    )
    message = f"“{key.name}” was revoked. It stopped working at once."
    return redirect(request, "/keys", flash=message)
