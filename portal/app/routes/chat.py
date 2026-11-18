"""The chat playground.

``GET  /chat``      the page. Conversations are kept in the browser, not on the server.
``POST /api/chat``  JSON ``{"messages": [{"role", "content"}], "temperature"?, "max_tokens"?,
                    "reasoning_effort"?}``; answers with a server-sent event stream of
                    ``data: <json>`` lines, each with a ``type``:

    reasoning  {"delta"}                          the model thinking, as it arrives
    content    {"delta"}                          the answer, as it arrives
    usage      {"prompt_tokens", "completion_tokens"}
    error      {"code", "message", "retry_after"?}  code is rate_limited | model_offline |
                                                     bad_request | gateway_error
    done       {}                                  always last

The model is called with the user's own playground key, so the playground is
rate-limited and metered exactly like API traffic.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

from app.deps import GatewayDep, RequiredUser, StatusDep, UsersDep
from app.gateway import CHAT_ERROR_MESSAGES, GatewayClient, GatewayError
from app.status import ModelStatusService
from app.templating import error_response, render
from app.users import AccountDisabled, User, UserService

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGES = 60
MAX_CONTENT = 16_000
HISTORY_BUDGET = 48_000  # characters of history sent to the model, newest first


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=16, le=8192)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"


def trim_history(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """The conversation as sent upstream: system prompts, then as much recent history as fits."""
    system = [m for m in messages if m.role == "system"]
    turns = [m for m in messages if m.role != "system"]
    kept: list[ChatMessage] = []
    used = 0
    for message in reversed(turns):
        used += len(message.content)
        if kept and used > HISTORY_BUDGET:
            break
        kept.append(message)
    return [{"role": m.role, "content": m.content} for m in [*system, *reversed(kept)]]


def _event(kind: str, **fields: Any) -> str:
    return f"data: {json.dumps({'type': kind, **fields})}\n\n"


def _error_event(exc: GatewayError) -> str:
    return _event("error", code=exc.kind, message=exc.public_message, retry_after=exc.retry_after)


async def _deltas(
    gateway: GatewayClient, api_key: str, payload: dict[str, Any]
) -> AsyncIterator[str]:
    """One upstream call, turned into this page's events."""
    async with contextlib.aclosing(gateway.chat_stream(api_key, payload)) as chunks:
        async for chunk in chunks:
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if reasoning := delta.get("reasoning_content"):
                    yield _event("reasoning", delta=reasoning)
                if content := delta.get("content"):
                    yield _event("content", delta=content)
            if usage := chunk.get("usage"):
                yield _event(
                    "usage",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )


async def _stream(
    user: User,
    payload: dict[str, Any],
    gateway: GatewayClient,
    users: UserService,
    status: ModelStatusService,
) -> AsyncIterator[str]:
    cached = status.cached
    if cached is not None and cached.state == "offline":
        status.refresh_in_background()
        yield _event("error", code="model_offline", message=CHAT_ERROR_MESSAGES["model_offline"])
        yield _event("done")
        return
    try:
        api_key = (await users.ensure_provisioned(user)).playground_key
        started = False
        try:
            async for event in _deltas(gateway, api_key, payload):
                started = True
                yield event
        except GatewayError as exc:
            if started or exc.kind != "auth":
                raise
            # The gateway no longer knows the playground key: issue a new one and try once more.
            fresh = await users.ensure_provisioned(user, reissue_playground_key=True)
            async for event in _deltas(gateway, fresh.playground_key, payload):
                yield event
    except GatewayError as exc:
        logger.warning("Chat failed for user %s: %s (status %s)", user.id, exc.code, exc.status)
        if exc.kind == "model_offline":
            status.invalidate()
        yield _error_event(exc)
    except AccountDisabled:
        yield _event("error", code="gateway_error", message="This account has been disabled.")
    yield _event("done")


@router.get("/chat")
async def chat_page(request: Request, user: RequiredUser) -> Response:
    return render(request, "chat.html")


@router.post("/api/chat")
async def chat_stream(
    request: Request,
    body: ChatRequest,
    user: RequiredUser,
    gateway: GatewayDep,
    users: UsersDep,
    status: StatusDep,
) -> Response:
    if body.messages[-1].role != "user":
        return error_response(request, 400, "invalid_request", "The last message must be yours.")
    payload = {
        "messages": trim_history(body.messages),
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "reasoning_effort": body.reasoning_effort,
    }
    return StreamingResponse(
        _stream(user, payload, gateway, users, status),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
