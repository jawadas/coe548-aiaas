"""API documentation (placeholder until the feature is built). Public: no sign-in needed."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.templating import render

router = APIRouter()


@router.get("/docs")
async def docs_page(request: Request) -> Response:
    return render(request, "stub.html", {"title": "API documentation"})
