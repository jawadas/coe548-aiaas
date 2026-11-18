"""Usage: totals, a daily chart, a per-key breakdown and the recent requests.

``GET /usage?range=7|14|30``       the page
``GET /usage/export.csv?range=``   the daily numbers as a CSV download
"""

from __future__ import annotations

import csv
import io
import logging

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.deps import GatewayDep, RequiredUser, SettingsDep, UsersDep
from app.gateway import GatewayError
from app.services import usage as usage_service
from app.templating import error_response, render

logger = logging.getLogger(__name__)
router = APIRouter()

RECENT_REQUESTS = 15


def _range(value: int | None) -> int:
    return value if value in usage_service.RANGES else usage_service.DEFAULT_RANGE


@router.get("/usage")
async def usage_page(
    request: Request,
    user: RequiredUser,
    gateway: GatewayDep,
    users: UsersDep,
    settings: SettingsDep,
    range: int | None = None,
) -> Response:
    days_count = _range(range)
    start, end = usage_service.date_range(days_count)
    gateway_down = False
    days, keys, recent = [], [], []
    try:
        days = await gateway.daily_usage(user.gateway_user_id, start, end)
        keys = await gateway.list_keys(user.gateway_user_id)
        recent, _ = await gateway.recent_requests(user.gateway_user_id, 1, RECENT_REQUESTS)
    except GatewayError as exc:
        logger.warning("Usage page without gateway data: %s", exc.code)
        gateway_down = True

    names = {key.token: key.name for key in keys}
    context = {
        "gateway_down": gateway_down,
        "range": days_count,
        "ranges": usage_service.RANGES,
        "start": start,
        "end": end,
        "totals": usage_service.totals(days),
        "chart": usage_service.build_chart(days) if days else None,
        "days": days,
        "by_key": usage_service.by_key(days, keys),
        "recent": [(entry, names.get(entry.key_token or "", "Revoked key")) for entry in recent],
        "key_rpm_limit": settings.key_rpm_limit,
        "key_tpm_limit": settings.key_tpm_limit,
        "user_limits": users.limits_for(user),
    }
    return render(request, "usage.html", context)


@router.get("/usage/export.csv")
async def usage_export(
    request: Request,
    user: RequiredUser,
    gateway: GatewayDep,
    range: int | None = None,
) -> Response:
    start, end = usage_service.date_range(_range(range))
    try:
        days = await gateway.daily_usage(user.gateway_user_id, start, end)
    except GatewayError:
        return error_response(request, 503)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "requests",
            "successful",
            "failed",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ]
    )
    for day in days:
        writer.writerow(
            [
                day.date.isoformat(),
                day.requests,
                day.successful,
                day.failed,
                day.prompt_tokens,
                day.completion_tokens,
                day.total_tokens,
            ]
        )
    filename = f"aiaas-usage-{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
