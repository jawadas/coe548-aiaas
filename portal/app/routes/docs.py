"""The API documentation. Public: no sign-in needed."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.deps import SettingsDep
from app.templating import render

router = APIRouter()


@router.get("/docs")
async def docs_page(request: Request, settings: SettingsDep) -> Response:
    context = {
        "base_url": f"{settings.public_url}/v1",
        "key_rpm_limit": settings.key_rpm_limit,
        "key_tpm_limit": settings.key_tpm_limit,
        "user_rpm_limit": settings.user_rpm_limit,
        "user_tpm_limit": settings.user_tpm_limit,
    }
    return render(request, "docs.html", context)
