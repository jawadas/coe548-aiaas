"""Account (placeholder until the feature is built)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.deps import RequiredUser
from app.templating import render

router = APIRouter()


@router.get("/account")
async def account_page(request: Request, user: RequiredUser) -> Response:
    return render(request, "stub.html", {"title": "Account"})
