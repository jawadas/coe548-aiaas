"""The signed-in home page: today's numbers, a 14-day chart, the quickstart and recent keys."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.deps import GatewayDep, RequiredUser, SettingsDep, UsersDep
from app.gateway import GatewayError, KeyInfo
from app.services import usage as usage_service
from app.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

CHART_DAYS = 14
RECENT_KEYS = 3


def api_keys(keys: list[KeyInfo]) -> list[KeyInfo]:
    """The keys the user made, newest first (the playground key is the portal's own)."""
    own = [key for key in keys if key.kind == "api"]
    return sorted(own, key=lambda key: (key.created_at is not None, key.created_at), reverse=True)


@router.get("/overview")
async def overview_page(
    request: Request,
    user: RequiredUser,
    gateway: GatewayDep,
    users: UsersDep,
    settings: SettingsDep,
) -> Response:
    start, end = usage_service.date_range(CHART_DAYS)
    gateway_down = False
    days, keys = [], []
    try:
        days = await gateway.daily_usage(user.gateway_user_id, start, end)
        keys = api_keys(await gateway.list_keys(user.gateway_user_id))
    except GatewayError as exc:
        logger.warning("Overview without gateway data: %s", exc.code)
        gateway_down = True

    today = days[-1] if days else None
    yesterday = days[-2] if len(days) > 1 else None
    limits = users.limits_for(user)
    active = sum(1 for key in keys if not key.blocked)
    context = {
        "gateway_down": gateway_down,
        "today": today,
        "change": usage_service.change_percent(today.requests, yesterday.requests)
        if today and yesterday
        else None,
        "totals": usage_service.totals(days),
        "chart": usage_service.build_chart(days) if days else None,
        "chart_days": CHART_DAYS,
        "keys": keys[:RECENT_KEYS],
        "key_count": len(keys),
        "active_keys": active,
        "max_keys": limits.max_keys,
        "key_rpm_limit": settings.key_rpm_limit,
        "is_new": not keys and not any(day.requests for day in days),
        "base_url": f"{settings.public_url}/v1",
    }
    return render(request, "overview.html", context)
