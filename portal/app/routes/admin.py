"""Admin area (placeholder until the feature is built). Admins only."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.deps import AdminUser
from app.templating import render

router = APIRouter()


@router.get("/admin")
async def admin_page(request: Request, user: AdminUser) -> Response:
    return render(request, "stub.html", {"title": "Admin"})
