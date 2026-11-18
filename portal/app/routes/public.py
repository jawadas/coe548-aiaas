"""Public endpoints: the landing page, the health probes and the model status.

``GET /``            landing page (links to sign-up, sign-in and the docs)
``GET /healthz``     liveness: 200 ``{"status": "ok"}`` while the process runs
``GET /readyz``      readiness: 200 if the database answers, 503 otherwise; also
                     reports whether the gateway is reachable, without failing on it
``GET /api/status``  ``{"state": "online" | "offline" | "unknown", "checked_at", "latency_ms"}``
                     for the status indicator; cached by ``ModelStatusService``
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from app import db
from app.deps import GatewayDep, PoolDep, StatusDep
from app.templating import render

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def landing(request: Request) -> Response:
    return render(request, "public/landing.html")


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.api_route("/readyz", methods=["GET", "HEAD"])
async def readyz(pool: PoolDep, gateway: GatewayDep) -> JSONResponse:
    database_ok, gateway_ok = await asyncio.gather(db.ping(pool), gateway.ready())
    body = {
        "status": "ready" if database_ok else "unavailable",
        "database": "ok" if database_ok else "unreachable",
        "gateway": "ok" if gateway_ok else "unreachable",
    }
    return JSONResponse(body, status_code=200 if database_ok else 503)


@router.get("/api/status")
async def model_status(status: StatusDep) -> JSONResponse:
    """Only these three fields: the health detail can name the upstream GPU host."""
    health = await status.get()
    return JSONResponse(
        {
            "state": health.state,
            "checked_at": health.checked_at.isoformat(timespec="seconds"),
            "latency_ms": health.latency_ms,
        },
        headers={"Cache-Control": "no-store"},
    )
