"""An in-memory stand-in for the LiteLLM gateway.

``FakeGateway`` has the same public interface as ``app.gateway.GatewayClient``
(``test_fakes.py`` compares the two signature by signature), returns the same
dataclasses and raises the same exceptions, so the portal cannot tell the
difference. The ``gateway`` fixture hands one to the application under test.

What it models
    Users, keys (limits, blocked state, display names), a request log from
    which the usage reports are computed, model health, and a scripted
    streaming chat reply: reasoning deltas, content deltas, a finish chunk and
    a final usage chunk, exactly in the order the real gateway sends them.

Failure switches (plain attributes; flip them at any point in a test)
    ``down = True``            the gateway cannot be reached: every call raises
                               ``GatewayUnavailable``, ``ready()`` is false and
                               ``health()`` reports ``unknown``
    ``model_offline = True``   the gateway is fine but the GPU pod is not:
                               ``health()`` reports ``offline`` and chats fail
                               the way ``offline_as`` says (``"error"``: 500,
                               ``"not_found"``: 404, ``"cooldown"``: the 429
                               that must not be mistaken for a rate limit)
    ``rate_limited = True``    chats are refused with 429 ``throttling_error``
                               and ``retry_after=60``
    ``fail("method", error)``  the next call of that one method raises ``error``

Shaping the model's answer
    ``reply = ChatScript(content="...", reasoning="...", break_after=3)``
    A user message containing ``[[error]]`` fails with an upstream 500, like
    the mock model of the local stack.

Seeing what the portal did
    ``calls``          names of the methods called, in order (``count(name)``)
    ``chats``          the payload of every chat request
    ``users``, ``keys``  the stored state (``keys_of(user_id)``)
    ``secret_of(token)`` a key's secret, which the real gateway never gives back
    ``open_chats``     streams still open; 0 once every consumer has finished or stopped
    ``stopped_chats``  streams the consumer closed before the end

Seeding usage
    ``add_usage(user_id, token, day, requests=3, prompt_tokens=30, completion_tokens=90)``
    ``record_request(user_id, token, status="failure", error_code="429")``

Every upstream-looking error text contains ``UPSTREAM_HOST``. Tests use it to
prove that the address of the GPU pod never reaches a page or an API response.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import secrets
import string
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.gateway import (
    REQUEST_LOG_DAYS,
    DayUsage,
    GatewayError,
    GatewayNotFound,
    GatewayUnavailable,
    KeyInfo,
    KeyKind,
    KeyUsage,
    ModelHealth,
    NewKey,
    RequestLog,
    UserUsage,
    key_alias,
)

UPSTREAM_HOST = "fake-gpu-pod.invalid"
RATE_LIMIT_RETRY_AFTER = 60
_SECRET_ALPHABET = string.ascii_letters + string.digits


@dataclass(slots=True)
class FakeUser:
    """A gateway user: the owner of keys and the carrier of the per-user limits."""

    user_id: str
    email: str | None
    rpm_limit: int | None
    tpm_limit: int | None


@dataclass(slots=True)
class FakeKey:
    """A gateway key, including the secret the real gateway shows only once."""

    token: str
    secret: str = field(repr=False)
    user_id: str = ""
    alias: str = ""
    name: str = ""
    kind: str = "api"
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    models: tuple[str, ...] = ()
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    last_active: dt.datetime | None = None
    blocked: bool = False

    @property
    def masked(self) -> str:
        return f"sk-...{self.secret[-4:]}"

    def info(self) -> KeyInfo:
        """This key as ``GatewayClient`` would describe it."""
        return KeyInfo(
            token=self.token,
            name=self.name,
            masked=self.masked,
            kind=self.kind,
            alias=self.alias,
            created_at=self.created_at,
            last_active=self.last_active,
            rpm_limit=self.rpm_limit,
            tpm_limit=self.tpm_limit,
            blocked=self.blocked,
            user_id=self.user_id,
        )


@dataclass(frozen=True, slots=True)
class FakeRequest:
    """One row of the gateway's request log."""

    id: str
    started_at: dt.datetime
    duration_ms: int
    status: str
    user_id: str | None
    token: str | None
    alias: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    error_code: str | None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_log(self) -> RequestLog:
        return RequestLog(
            id=self.id,
            started_at=self.started_at,
            duration_ms=self.duration_ms,
            status=self.status,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            key_token=self.token,
            key_alias=self.alias,
            error_code=self.error_code,
        )


@dataclass(frozen=True, slots=True)
class ChatScript:
    """What the fake model answers.

    ``reasoning`` is streamed first (as ``reasoning_content`` deltas), then
    ``content``. ``delay`` seconds pass before each chunk. ``break_after=n``
    drops the connection after ``n`` chunks, as a crashing pod would.
    """

    content: str = "Hello! This is the fake model speaking."
    reasoning: str = "The user greeted me. I should greet them back."
    finish_reason: str = "stop"
    delay: float = 0.0
    break_after: int | None = None


def _pieces(text: str) -> list[str]:
    """Split text into token-like pieces that keep their whitespace."""
    return re.findall(r"\s*\S+\s*", text)


def _count_words(messages: Any) -> int:
    texts = [str(message.get("content", "")) for message in messages if isinstance(message, dict)]
    return max(1, sum(len(text.split()) for text in texts))


def chat_chunks(model: str, payload: Mapping[str, Any], script: ChatScript) -> list[dict[str, Any]]:
    """The chunks of one streamed completion, shaped like the gateway's.

    A role chunk, the reasoning deltas, the content deltas, a chunk carrying
    ``finish_reason`` and last a chunk with empty ``choices`` and ``usage``.
    ``max_tokens`` in the payload cuts the answer short (``finish_reason: length``).
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(choices: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices,
            **extra,
        }

    def delta(body: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        choice: dict[str, Any] = {"index": 0, "delta": body}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return chunk([choice])

    reasoning, content = _pieces(script.reasoning), _pieces(script.content)
    finish_reason = script.finish_reason
    limit = payload.get("max_tokens")
    if isinstance(limit, int) and 0 < limit < len(reasoning) + len(content):
        reasoning, content = reasoning[:limit], content[: max(0, limit - len(reasoning))]
        finish_reason = "length"
    prompt_tokens = _count_words(payload.get("messages") or [])
    completion_tokens = len(reasoning) + len(content)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "completion_tokens_details": {"reasoning_tokens": len(reasoning)},
    }
    return [
        delta({"role": "assistant", "content": ""}),
        *(delta({"reasoning_content": piece}) for piece in reasoning),
        *(delta({"content": piece}) for piece in content),
        delta({}, finish_reason),
        chunk([], usage=usage),
    ]


class FakeGateway:
    """Drop-in replacement for ``GatewayClient``. See the module docstring."""

    def __init__(self, model: str = "gpt-oss-20b") -> None:
        self.model = model
        self.users: dict[str, FakeUser] = {}
        self.keys: dict[str, FakeKey] = {}
        self.log: list[FakeRequest] = []
        self.calls: list[str] = []
        self.chats: list[dict[str, Any]] = []
        self.canned: dict[tuple[str, str], Any] = {}
        self.reply = ChatScript()
        self.latency_ms = 180
        self.down = False
        self.model_offline = False
        self.offline_as = "error"
        self.rate_limited = False
        self.open_chats = 0
        self.stopped_chats = 0
        self.closed = False
        self._failures: dict[str, list[Exception]] = {}
        self._last_time = dt.datetime.min.replace(tzinfo=dt.UTC)

    # -- test controls ------------------------------------------------------

    def fail(self, method: str, error: Exception, times: int = 1) -> None:
        """Make the next ``times`` calls of ``method`` raise ``error``."""
        self._failures.setdefault(method, []).extend([error] * times)

    def count(self, method: str) -> int:
        """How many times ``method`` has been called."""
        return self.calls.count(method)

    def secret_of(self, token: str) -> str:
        return self.keys[token].secret

    def keys_of(self, user_id: str) -> list[FakeKey]:
        return [key for key in self.keys.values() if key.user_id == user_id]

    def record_request(
        self,
        user_id: str | None,
        token: str | None = None,
        *,
        when: dt.datetime | None = None,
        status: str = "success",
        prompt_tokens: int = 10,
        completion_tokens: int = 20,
        duration_ms: int = 250,
        error_code: str | None = None,
    ) -> FakeRequest:
        """Append one row to the request log (and so to the usage reports)."""
        key = self.keys.get(token or "")
        failed = status != "success"
        row = FakeRequest(
            id=str(uuid.uuid4()),
            started_at=when or self._now(),
            duration_ms=duration_ms,
            status=status,
            user_id=user_id,
            token=token,
            alias=key.alias if key else None,
            model=self.model,
            prompt_tokens=0 if failed else prompt_tokens,
            completion_tokens=0 if failed else completion_tokens,
            error_code=error_code,
        )
        self.log.append(row)
        return row

    def add_usage(
        self,
        user_id: str,
        token: str | None,
        day: dt.date,
        *,
        requests: int = 1,
        failed: int = 0,
        prompt_tokens: int = 10,
        completion_tokens: int = 20,
        duration_ms: int = 250,
    ) -> None:
        """Seed a day of usage: ``requests`` successful calls sharing the tokens, plus failures.

        The rows are stamped at noon UTC of ``day``, a few seconds apart.
        """
        noon = dt.datetime.combine(day, dt.time(12), tzinfo=dt.UTC)
        for index in range(requests):
            first = index == 0
            self.record_request(
                user_id,
                token,
                when=noon + dt.timedelta(seconds=index),
                prompt_tokens=prompt_tokens // requests + (prompt_tokens % requests) * first,
                completion_tokens=completion_tokens // requests
                + (completion_tokens % requests) * first,
                duration_ms=duration_ms,
            )
        for index in range(failed):
            self.record_request(
                user_id,
                token,
                when=noon + dt.timedelta(seconds=requests + index),
                status="failure",
                duration_ms=5,
                error_code="500",
            )

    # -- plumbing -----------------------------------------------------------

    def _now(self) -> dt.datetime:
        """The current time, strictly increasing so that creation order is stable."""
        now = max(dt.datetime.now(dt.UTC), self._last_time + dt.timedelta(microseconds=1))
        self._last_time = now
        return now

    def _guard(self, method: str, *, raises_when_down: bool = True) -> None:
        """Record the call, then apply a scripted failure or the ``down`` switch."""
        self.calls.append(method)
        scripted = self._failures.get(method)
        if scripted:
            raise scripted.pop(0)
        if self.down and raises_when_down:
            raise GatewayUnavailable(
                None, "gateway_unreachable", "Could not connect to the gateway"
            )

    def _user(self, user_id: str) -> FakeUser:
        user = self.users.get(user_id)
        if user is None:
            raise GatewayNotFound(404, "user_not_found", f"User {user_id} not found")
        return user

    def _key(self, token: str) -> FakeKey:
        key = self.keys.get(token)
        if key is None:
            raise GatewayNotFound(404, "not_found_error", "Key not found.")
        return key

    def _authenticate(self, api_key: str) -> FakeKey:
        """The key behind a secret, or the 401 the gateway answers with."""
        if not api_key.startswith("sk-"):
            raise GatewayError(
                401,
                "auth_error",
                "Authentication Error, LiteLLM Virtual Key expected. "
                "Received key does not start with 'sk-'.",
            )
        key = self.keys.get(hashlib.sha256(api_key.encode()).hexdigest())
        if key is None:
            raise GatewayError(
                401, "token_not_found_in_db", "Authentication Error, Invalid API key provided."
            )
        if key.blocked:
            raise GatewayError(
                401,
                "auth_error",
                "Authentication Error, Key is blocked. "
                "Update via `/key/unblock` if you're an admin.",
            )
        return key

    def _offline_error(self) -> GatewayError:
        upstream = f"Hosted_vllmException - Cannot connect to host {UPSTREAM_HOST}:443"
        if self.offline_as == "cooldown":
            return GatewayError(
                429,
                "all_deployments_in_cooldown",
                f"No deployments available for selected model, Try again in 5 seconds. "
                f"Passed model={self.model}.",
            )
        if self.offline_as == "not_found":
            return GatewayError(
                404, "not_found_error", f"litellm.NotFoundError: NotFoundError: {upstream}"
            )
        return GatewayError(
            500,
            "internal_server_error",
            f"litellm.InternalServerError: InternalServerError: {upstream}",
        )

    def _check_chat(self, key: FakeKey, payload: Mapping[str, Any]) -> None:
        """Refuse a chat request for the same reasons, in the same order, as the gateway."""
        model = payload.get("model", self.model)
        if model not in key.models:
            raise GatewayError(
                403,
                "key_model_access_denied",
                f"The requested model '{model}' is not available for this API key.",
            )
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GatewayError(400, "invalid_request_error", "messages: field required")
        if self.rate_limited:
            raise GatewayError(
                429,
                "throttling_error",
                f"Rate limit exceeded for api_key: {key.token}. Limit type: requests. "
                f"Current limit: {key.rpm_limit}, Remaining: 0. Limit resets at: soon",
                retry_after=RATE_LIMIT_RETRY_AFTER,
            )
        if self.model_offline:
            raise self._offline_error()
        if "[[error]]" in str(messages[-1].get("content", "")):
            raise GatewayError(
                500,
                "internal_server_error",
                "litellm.InternalServerError: InternalServerError: Hosted_vllmException - "
                '{"error": {"message": "Mock upstream failure"}}',
            )

    # -- GatewayClient interface: plumbing ------------------------------------

    async def aclose(self) -> None:
        self.closed = True

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        api_key: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 (mirrors GatewayClient.request)
    ) -> Any:
        """The escape hatch. Answers from ``canned[(METHOD, path)]``.

        A canned value may be a callable taking ``(json, params)``. A route
        nobody prepared is a test bug, reported as such.
        """
        self._guard("request")
        try:
            canned = self.canned[(method.upper(), path)]
        except KeyError:
            raise AssertionError(
                f"FakeGateway has no canned response for {method.upper()} {path}. "
                f"Set gateway.canned[({method.upper()!r}, {path!r})] in the test."
            ) from None
        return canned(json, params) if callable(canned) else canned

    # -- users ----------------------------------------------------------------

    async def ensure_user(self, user_id: str, email: str, rpm_limit: int, tpm_limit: int) -> None:
        self._guard("ensure_user")
        self.users[user_id] = FakeUser(user_id, email, rpm_limit, tpm_limit)

    async def update_user_limits(self, user_id: str, rpm_limit: int, tpm_limit: int) -> None:
        self._guard("update_user_limits")
        user = self._user(user_id)
        user.rpm_limit, user.tpm_limit = rpm_limit, tpm_limit

    async def delete_user(self, user_id: str) -> None:
        self._guard("delete_user")
        self.users.pop(user_id, None)
        for key in self.keys_of(user_id):
            del self.keys[key.token]

    # -- keys -----------------------------------------------------------------

    async def create_key(
        self, user_id: str, name: str, kind: KeyKind, rpm_limit: int, tpm_limit: int
    ) -> NewKey:
        self._guard("create_key")
        self._user(user_id)
        secret = "sk-" + "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(22))
        key = FakeKey(
            token=hashlib.sha256(secret.encode()).hexdigest(),
            secret=secret,
            user_id=user_id,
            alias=key_alias(kind, user_id, name),
            name=name,
            kind=kind,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            models=(self.model,),
            created_at=self._now(),
        )
        self.keys[key.token] = key
        return NewKey(secret=secret, info=key.info())

    async def list_keys(self, user_id: str) -> list[KeyInfo]:
        self._guard("list_keys")
        keys = sorted(self.keys_of(user_id), key=lambda key: key.created_at, reverse=True)
        return [key.info() for key in keys]

    async def get_key(self, token: str) -> KeyInfo | None:
        self._guard("get_key")
        key = self.keys.get(token)
        return None if key is None else key.info()

    async def delete_key(self, token: str) -> None:
        self._guard("delete_key")
        self.keys.pop(token, None)

    async def block_key(self, token: str) -> None:
        self._guard("block_key")
        self._key(token).blocked = True

    async def unblock_key(self, token: str) -> None:
        self._guard("unblock_key")
        self._key(token).blocked = False

    async def rename_key(self, token: str, name: str) -> None:
        self._guard("rename_key")
        self._key(token).name = name

    async def update_key_limits(self, token: str, rpm_limit: int, tpm_limit: int) -> None:
        self._guard("update_key_limits")
        key = self._key(token)
        key.rpm_limit, key.tpm_limit = rpm_limit, tpm_limit

    # -- usage ----------------------------------------------------------------

    def _key_usage(self, token: str, rows: list[FakeRequest]) -> KeyUsage:
        alias = next((row.alias for row in rows if row.alias), None)
        return KeyUsage(
            token=token,
            name=alias or f"{token[:8]}...",
            requests=len(rows),
            successful=sum(row.status == "success" for row in rows),
            failed=sum(row.status != "success" for row in rows),
            prompt_tokens=sum(row.prompt_tokens for row in rows),
            completion_tokens=sum(row.completion_tokens for row in rows),
            total_tokens=sum(row.total_tokens for row in rows),
        )

    def _day_usage(self, day: dt.date, rows: list[FakeRequest]) -> DayUsage:
        if not rows:
            return DayUsage.empty(day)
        timed = [row.duration_ms for row in rows if row.status == "success"]
        tokens = sorted({row.token for row in rows if row.token})
        by_key = [
            self._key_usage(token, [row for row in rows if row.token == token]) for token in tokens
        ]
        by_key.sort(key=lambda usage: (-usage.total_tokens, -usage.requests, usage.name))
        return DayUsage(
            date=day,
            requests=len(rows),
            successful=len(timed),
            failed=len(rows) - len(timed),
            prompt_tokens=sum(row.prompt_tokens for row in rows),
            completion_tokens=sum(row.completion_tokens for row in rows),
            total_tokens=sum(row.total_tokens for row in rows),
            spend=0.0,
            avg_latency_ms=round(sum(timed) / len(timed), 1) if timed else None,
            by_key=by_key,
        )

    async def daily_usage(
        self, user_id: str | None, start: dt.date, end: dt.date
    ) -> list[DayUsage]:
        self._guard("daily_usage")
        if start > end:
            raise ValueError("start must not be after end")
        rows = [row for row in self.log if user_id is None or row.user_id == user_id]
        days = [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]
        return [
            self._day_usage(day, [row for row in rows if row.started_at.date() == day])
            for day in days
        ]

    async def usage_by_user(self, start: dt.date, end: dt.date) -> list[UserUsage]:
        self._guard("usage_by_user")
        rows = [row for row in self.log if start <= row.started_at.date() <= end]
        users = []
        for user_id in sorted({row.user_id or "" for row in rows}):
            mine = [row for row in rows if (row.user_id or "") == user_id]
            known = self.users.get(user_id)
            users.append(
                UserUsage(
                    user_id=user_id,
                    email=known.email if known else None,
                    requests=len(mine),
                    successful=sum(row.status == "success" for row in mine),
                    failed=sum(row.status != "success" for row in mine),
                    prompt_tokens=sum(row.prompt_tokens for row in mine),
                    completion_tokens=sum(row.completion_tokens for row in mine),
                    total_tokens=sum(row.total_tokens for row in mine),
                    spend=0.0,
                )
            )
        return sorted(users, key=lambda user: (-user.total_tokens, -user.requests, user.user_id))

    async def recent_requests(
        self,
        user_id: str | None,
        page: int = 1,
        page_size: int = 20,
        *,
        days: int = REQUEST_LOG_DAYS,
    ) -> tuple[list[RequestLog], int]:
        self._guard("recent_requests")
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        rows = [
            row
            for row in self.log
            if row.started_at >= since and (user_id is None or row.user_id == user_id)
        ]
        rows.sort(key=lambda row: row.started_at, reverse=True)
        first = (page - 1) * page_size
        return [row.as_log() for row in rows[first : first + page_size]], len(rows)

    # -- health and models ------------------------------------------------------

    async def ready(self) -> bool:
        self._guard("ready", raises_when_down=False)
        return not self.down

    async def health(self) -> ModelHealth:
        self._guard("health", raises_when_down=False)
        now = dt.datetime.now(dt.UTC)
        if self.down:
            return ModelHealth("unknown", None, now, "Could not connect to the gateway")
        if self.model_offline:
            return ModelHealth("offline", None, now, self._offline_error().message)
        return ModelHealth("online", self.latency_ms, now, None)

    async def list_models(self, api_key: str | None = None) -> list[str]:
        self._guard("list_models")
        if api_key is not None:
            return list(self._authenticate(api_key).models)
        return [self.model]

    # -- inference ----------------------------------------------------------------

    async def chat_stream(
        self, api_key: str, payload: Mapping[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self._guard("chat_stream")
        self.chats.append(dict(payload))
        started = self._now()
        key: FakeKey | None = None
        try:
            key = self._authenticate(api_key)
            self._check_chat(key, payload)
        except GatewayError as exc:
            self.record_request(
                key.user_id if key else None,
                key.token if key else None,
                when=started,
                status="failure",
                duration_ms=5,
                error_code=str(exc.status),
            )
            raise
        script = self.reply
        chunks = chat_chunks(self.model, {"model": self.model, **payload}, script)
        self.open_chats += 1
        try:
            for index, chunk in enumerate(chunks):
                if script.break_after is not None and index >= script.break_after:
                    raise GatewayUnavailable(
                        None,
                        "gateway_connection_lost",
                        "The connection to the gateway was interrupted",
                    )
                if script.delay:
                    await asyncio.sleep(script.delay)
                yield chunk
        except GeneratorExit:
            self.stopped_chats += 1
            raise
        finally:
            self.open_chats -= 1
        usage = chunks[-1]["usage"]
        key.last_active = self._now()
        self.record_request(
            key.user_id,
            key.token,
            when=started,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            duration_ms=self.latency_ms,
        )
