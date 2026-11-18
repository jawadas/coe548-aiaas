"""Client for the LiteLLM gateway.

Two kinds of calls go through here:

* admin calls (users, keys, usage, health), authenticated with the master key;
* model inference on behalf of a user, authenticated with that user's own key.

The master key is only ever attached to a request built from a path relative
to ``base_url``. Redirects are not followed and proxy environment variables are
ignored, so the key cannot be sent anywhere but the gateway.

Error handling in one place: every failure surfaces as a ``GatewayError``
(or one of its two subclasses). No ``httpx`` exception escapes this module.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import string
import time
import unicodedata
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Self

import httpx

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 3.0
ADMIN_TIMEOUT: Final = 15.0
HEALTH_TIMEOUT: Final = 8.0
READINESS_TIMEOUT: Final = 3.0
STREAM_READ_TIMEOUT: Final = 300.0

PAGE_SIZE: Final = 100  # the most /key/list and the per-user usage route accept
ACTIVITY_PAGE_SIZE: Final = 1000
MAX_PAGES: Final = 200
ALIAS_ATTEMPTS: Final = 3
MAX_ERROR_MESSAGE: Final = 2000
MAX_HEALTH_DETAIL: Final = 300
REQUEST_LOG_DAYS: Final = 30

KeyKind = Literal["api", "playground"]
HealthState = Literal["online", "offline", "unknown"]
ChatErrorKind = Literal["rate_limited", "model_offline", "auth", "bad_request", "gateway_error"]

# What to tell a user for each kind of chat failure. Safe to show to anyone:
# unlike ``GatewayError.message`` these never contain upstream details.
CHAT_ERROR_MESSAGES: Final[dict[ChatErrorKind, str]] = {
    "rate_limited": "You are sending requests too quickly. Wait a moment and try again.",
    "model_offline": (
        "The model is offline right now. The GPU runs on demand, so please try again later."
    ),
    "auth": "The gateway rejected this key. It may have been revoked or disabled.",
    "bad_request": "The model could not accept this request. Check the message and settings.",
    "gateway_error": "The gateway could not process the request. Please try again.",
}

_BAD_REQUEST_STATUSES: Final = frozenset({400, 413, 422})
_UPSTREAM_MARKERS: Final = ("hosted_vllm", "cannot connect to host")
_COOLDOWN_MARKER: Final = "no deployments available"
_ALIAS_ALPHABET: Final = string.ascii_lowercase + string.digits


def classify_chat_error(
    status: int | None, error_type: str | None, message: str | None
) -> ChatErrorKind:
    """Sort a failed chat call into the handful of cases a user interface cares about.

    ``status`` is the HTTP status (``None`` when the gateway gave no response),
    ``error_type`` the ``type`` field of the gateway's error body and ``message``
    its text. The order of the checks matters:

    * A 429 that says no deployment is available is the model being down
      (the gateway puts a dead upstream "in cooldown"), not the user's fault.
    * 401/403 are key problems; 400/413/422 are problems with the request,
      even when the text mentions the upstream.
    * Any other 429 is a genuine rate limit.
    * Anything else that carries the upstream's signature ("Hosted_vllmException",
      "Cannot connect to host") is the model, whatever the status.
    * 404, 408 and every 5xx on a chat call come from the upstream model
      (pod stopped, unreachable or failing).
    * No status and no upstream signature means the gateway itself could not
      be reached: that is a gateway error, not the model being offline.
    """
    kind = (error_type or "").lower()
    text = (message or "").lower()
    if kind == "all_deployments_in_cooldown" or _COOLDOWN_MARKER in text:
        return "model_offline"
    if status in (401, 403):
        return "auth"
    if status in _BAD_REQUEST_STATUSES:
        return "bad_request"
    if status == 429:
        return "rate_limited"
    if any(marker in text for marker in _UPSTREAM_MARKERS):
        return "model_offline"
    if status is None:
        return "gateway_error"
    if status in (404, 408) or status >= 500:
        return "model_offline"
    return "gateway_error"


class GatewayError(Exception):
    """The gateway refused a request, failed, or could not be reached.

    Attributes:
        status: HTTP status, or ``None`` when no response arrived.
        code: the gateway's error ``type`` (``throttling_error``,
            ``token_not_found_in_db`` ...), or ``http_<status>`` when it gave none.
        message: the gateway's own text. For logs and admins only: it can
            name the upstream GPU host. Show ``public_message`` to users instead.
        retry_after: seconds to wait, when the gateway sent ``Retry-After``.
    """

    def __init__(
        self, status: int | None, code: str, message: str, retry_after: int | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def kind(self) -> ChatErrorKind:
        """This error as classified by ``classify_chat_error``."""
        return classify_chat_error(self.status, self.code, self.message)

    @property
    def public_message(self) -> str:
        """A fixed, user-safe sentence for this error (no upstream details)."""
        return CHAT_ERROR_MESSAGES[self.kind]


class GatewayUnavailable(GatewayError):
    """The gateway is down, timed out, or one of its admin routes answered 5xx."""


class GatewayNotFound(GatewayError):
    """The gateway does not know the user or key that was asked for."""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_time(value: Any) -> dt.datetime | None:
    """Parse the gateway's ISO timestamps (``...Z`` or ``...+00:00``) as aware datetimes."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _parse_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def slugify(text: str, max_length: int = 24) -> str:
    """Reduce any text to lower-case ASCII words joined by hyphens (``key`` if nothing is left)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_length].strip("-") or "key"


def key_alias(kind: str, user_id: str, name: str) -> str:
    """Build a gateway key alias, which must be unique across the whole gateway.

    ``<kind>-<first 8 characters of the user's id>-<slug of the name>-<6 random characters>``.
    """
    short_user = re.sub(r"[^a-z0-9]", "", user_id.lower().removeprefix("portal-"))[:8] or "user"
    suffix = "".join(secrets.choice(_ALIAS_ALPHABET) for _ in range(6))
    return f"{kind}-{short_user}-{slugify(name)}-{suffix}"


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """One API key as the gateway describes it. Never contains the secret.

    ``token`` is the key's stable id (a sha256 hex digest), ``masked`` the
    ``sk-...abcd`` form for display, ``name`` what the user called it and
    ``kind`` whether the user made it (``api``), the portal made it for the
    playground (``playground``) or it came from elsewhere (``other``).
    """

    token: str
    name: str
    masked: str
    kind: str
    alias: str | None
    created_at: dt.datetime | None
    last_active: dt.datetime | None
    rpm_limit: int | None
    tpm_limit: int | None
    blocked: bool
    user_id: str | None

    @classmethod
    def from_gateway(cls, data: Mapping[str, Any], token: str | None = None) -> Self:
        """Build from a ``/key/list``, ``/key/generate`` or ``/key/info`` object.

        ``/key/info`` keeps the token outside the object, hence the parameter.
        """
        portal = _as_dict(_as_dict(data.get("metadata")).get("portal"))
        alias = data.get("key_alias") or None
        masked = data.get("key_name") or ""
        portal_name = portal.get("name")
        portal_kind = portal.get("kind")
        fallback_name = alias or masked or "Unnamed key"
        return cls(
            token=token or data.get("token") or "",
            name=portal_name if isinstance(portal_name, str) and portal_name else fallback_name,
            masked=masked,
            kind=portal_kind if portal_kind in ("api", "playground") else "other",
            alias=alias,
            created_at=_parse_time(data.get("created_at")),
            last_active=_parse_time(data.get("last_active")),
            rpm_limit=_as_optional_int(data.get("rpm_limit")),
            tpm_limit=_as_optional_int(data.get("tpm_limit")),
            blocked=bool(data.get("blocked")),
            user_id=data.get("user_id") or None,
        )


@dataclass(frozen=True, slots=True)
class NewKey:
    """A freshly created key. ``secret`` is shown once and cannot be fetched again."""

    secret: str = field(repr=False)
    info: KeyInfo

    @classmethod
    def from_gateway(cls, data: Mapping[str, Any]) -> Self:
        return cls(secret=data["key"], info=KeyInfo.from_gateway(data))


@dataclass(slots=True)
class _Totals:
    """Running sums of the gateway's usage metrics."""

    requests: int = 0
    successful: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    spend: float = 0.0
    response_ms: float = 0.0
    timed: int = 0

    def add(self, metrics: Mapping[str, Any]) -> None:
        self.requests += _as_int(metrics.get("api_requests"))
        self.successful += _as_int(metrics.get("successful_requests"))
        self.failed += _as_int(metrics.get("failed_requests"))
        self.prompt_tokens += _as_int(metrics.get("prompt_tokens"))
        self.completion_tokens += _as_int(metrics.get("completion_tokens"))
        self.total_tokens += _as_int(metrics.get("total_tokens"))
        self.spend += _as_float(metrics.get("spend"))
        self.response_ms += _as_float(metrics.get("total_response_time_ms"))
        # Only successful calls are timed; fall back to them on gateways without the field.
        timed = metrics.get("timed_requests", metrics.get("successful_requests"))
        self.timed += _as_int(timed)

    @property
    def avg_latency_ms(self) -> float | None:
        return round(self.response_ms / self.timed, 1) if self.timed else None


@dataclass(frozen=True, slots=True)
class KeyUsage:
    """One key's share of a day. ``name`` is the key's gateway alias."""

    token: str
    name: str
    requests: int
    successful: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_gateway(cls, token: str, entries: Iterable[Mapping[str, Any]]) -> Self:
        """Build from a day's ``breakdown.api_keys[token]`` objects (one per page the day spans)."""
        totals = _Totals()
        alias = None
        for entry in entries:
            totals.add(_as_dict(entry.get("metrics")))
            alias = _as_dict(entry.get("metadata")).get("key_alias") or alias
        return cls(
            token=token,
            name=alias if isinstance(alias, str) and alias else f"{token[:8]}...",
            requests=totals.requests,
            successful=totals.successful,
            failed=totals.failed,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            total_tokens=totals.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class DayUsage:
    """Usage for one UTC day. ``avg_latency_ms`` is ``None`` when nothing was timed."""

    date: dt.date
    requests: int
    successful: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    spend: float
    avg_latency_ms: float | None
    by_key: list[KeyUsage]

    @classmethod
    def empty(cls, day: dt.date) -> Self:
        return cls(day, 0, 0, 0, 0, 0, 0, 0.0, None, [])

    @classmethod
    def from_gateway(cls, day: dt.date, rows: Iterable[Mapping[str, Any]]) -> Self:
        """Build one day from the ``/user/daily/activity`` rows that belong to it.

        The gateway pages by (day, key, model), so a busy day can arrive as
        several rows, each with part of the metrics; they are added up here.
        """
        totals = _Totals()
        per_key: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            totals.add(_as_dict(row.get("metrics")))
            api_keys = _as_dict(_as_dict(row.get("breakdown")).get("api_keys"))
            for token, entry in api_keys.items():
                per_key.setdefault(token, []).append(_as_dict(entry))
        by_key = [KeyUsage.from_gateway(token, entries) for token, entries in per_key.items()]
        by_key.sort(key=lambda usage: (-usage.total_tokens, -usage.requests, usage.name))
        return cls(
            date=day,
            requests=totals.requests,
            successful=totals.successful,
            failed=totals.failed,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            total_tokens=totals.total_tokens,
            spend=totals.spend,
            avg_latency_ms=totals.avg_latency_ms,
            by_key=by_key,
        )


@dataclass(frozen=True, slots=True)
class UserUsage:
    """One gateway user's totals over a date range."""

    user_id: str
    email: str | None
    requests: int
    successful: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    spend: float

    @classmethod
    def from_gateway(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            user_id=str(data.get("user_id") or ""),
            email=data.get("user_email") or None,
            requests=_as_int(data.get("api_requests")),
            successful=_as_int(data.get("successful_requests")),
            failed=_as_int(data.get("failed_requests")),
            prompt_tokens=_as_int(data.get("prompt_tokens")),
            completion_tokens=_as_int(data.get("completion_tokens")),
            total_tokens=_as_int(data.get("total_tokens")),
            spend=_as_float(data.get("spend")),
        )


@dataclass(frozen=True, slots=True)
class RequestLog:
    """One request as recorded by the gateway. ``status`` is ``success`` or ``failure``."""

    id: str
    started_at: dt.datetime | None
    duration_ms: int | None
    status: str
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    key_token: str | None
    key_alias: str | None
    error_code: str | None

    @classmethod
    def from_gateway(cls, data: Mapping[str, Any]) -> Self:
        metadata = _as_dict(data.get("metadata"))
        error_code = _as_dict(metadata.get("error_information")).get("error_code")
        return cls(
            id=str(data.get("request_id") or ""),
            started_at=_parse_time(data.get("startTime")),
            duration_ms=_as_optional_int(data.get("request_duration_ms")),
            status=str(data.get("status") or metadata.get("status") or "success"),
            model=data.get("model_group") or data.get("model") or None,
            prompt_tokens=_as_int(data.get("prompt_tokens")),
            completion_tokens=_as_int(data.get("completion_tokens")),
            total_tokens=_as_int(data.get("total_tokens")),
            key_token=data.get("api_key") or None,
            key_alias=metadata.get("user_api_key_alias") or None,
            error_code=str(error_code) if error_code else None,
        )


@dataclass(frozen=True, slots=True)
class ModelHealth:
    """Whether the model answers.

    ``detail`` explains a non-online state. It is for admins only: it can
    contain the upstream's address. ``latency_ms`` is how long the gateway's
    own test call took, when the model is online.
    """

    state: HealthState
    latency_ms: int | None
    checked_at: dt.datetime
    detail: str | None

    @property
    def is_online(self) -> bool:
        return self.state == "online"

    @classmethod
    def from_gateway(cls, data: Mapping[str, Any], latency_ms: int) -> Self:
        """Build from the ``/health`` payload, which took ``latency_ms`` to arrive.

        The model is online when at least one deployment answered the
        gateway's test call: the gateway routes around the unhealthy ones.
        """
        unhealthy = [_as_dict(item) for item in data.get("unhealthy_endpoints") or []]
        now = dt.datetime.now(dt.UTC)
        if _as_int(data.get("healthy_count")) > 0:
            detail = f"{len(unhealthy)} deployment(s) unhealthy" if unhealthy else None
            return cls("online", latency_ms, now, detail)
        reason = str(unhealthy[0].get("error") or "").strip() if unhealthy else ""
        first_line = reason.splitlines()[0] if reason else "No healthy deployment"
        return cls("offline", None, now, first_line[:MAX_HEALTH_DETAIL])


def _days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    for offset in range((end - start).days + 1):
        yield start + dt.timedelta(days=offset)


def _parse_error_body(status: int, body: bytes) -> tuple[str, str]:
    """Pull ``(type, message)`` out of any error body the gateway produces.

    Seen in practice: ``{"error": {"message", "type", "code"}}`` on most routes,
    ``{"detail": {"error": "..."}}`` on a few admin routes, and plain text from
    whatever sits in between.
    """
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    payload = data.get("error", data.get("detail")) if isinstance(data, dict) else None
    code = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        code = payload.get("type")
    else:
        message = payload
    if message is not None and not isinstance(message, str):
        message = json.dumps(message)
    message = (message or text or f"HTTP {status}")[:MAX_ERROR_MESSAGE]
    return (code if isinstance(code, str) and code else f"http_{status}"), message


def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after", "")
    return int(value) if value.isdigit() else None


def _error_from_response(
    status: int, headers: Mapping[str, str], body: bytes, *, inference: bool
) -> GatewayError:
    """Turn an error response into the right exception.

    On admin routes a 404 means "no such user or key" and a 5xx means the
    gateway itself is in trouble. On inference routes those statuses describe
    the upstream model instead, so they stay plain ``GatewayError``s for
    ``classify_chat_error`` to interpret.
    """
    code, message = _parse_error_body(status, body)
    retry_after = _parse_retry_after(headers)
    if inference:
        return GatewayError(status, code, message, retry_after)
    if status == 404:
        return GatewayNotFound(status, code, message, retry_after)
    if status >= 500:
        return GatewayUnavailable(status, code, message, retry_after)
    return GatewayError(status, code, message, retry_after)


def _transport_error(exc: httpx.HTTPError) -> GatewayUnavailable:
    """Describe a failed connection without echoing addresses from the exception."""
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout):
        return GatewayUnavailable(None, "gateway_timeout", "The gateway did not answer in time")
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return GatewayUnavailable(None, "gateway_unreachable", "Could not connect to the gateway")
    return GatewayUnavailable(
        None, "gateway_connection_lost", "The connection to the gateway was interrupted"
    )


def _stream_error(chunk: Mapping[str, Any]) -> GatewayError:
    """An error the gateway reported inside a stream that had already started."""
    error = chunk.get("error")
    body = json.dumps({"error": error}).encode()
    raw_status = error.get("code") if isinstance(error, dict) else None
    status = int(raw_status) if str(raw_status).isdigit() else 500
    code, message = _parse_error_body(status, body)
    return GatewayError(status, code, message)


class GatewayClient:
    """Async client for the gateway's admin API and for model inference."""

    def __init__(
        self,
        base_url: str,
        master_key: str,
        model: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._master_key = master_key
        self.model = model
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(ADMIN_TIMEOUT, connect=CONNECT_TIMEOUT),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        """Release the HTTP connections (only if this client created them)."""
        if self._owns_http:
            await self._http.aclose()

    # -- plumbing ---------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("Gateway paths must start with '/'")
        return self._base_url + path

    def _headers(self, api_key: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key or self._master_key}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        api_key: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 (handed to httpx as its read timeout)
    ) -> Any:
        """Call any gateway route and return its decoded JSON body.

        Uses the master key unless ``api_key`` is given. Raises ``GatewayNotFound``
        for a 404 and ``GatewayUnavailable`` for connection problems and 5xx on
        admin routes; ``/v1/`` (inference) routes raise a plain ``GatewayError``
        for every status so that ``classify_chat_error`` can interpret them.
        """
        started = time.perf_counter()
        try:
            response = await self._http.request(
                method,
                self._url(path),
                json=json,
                params=params,
                headers=self._headers(api_key),
                timeout=httpx.Timeout(timeout or ADMIN_TIMEOUT, connect=CONNECT_TIMEOUT),
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("%s %s -> %d in %.0f ms", method, path, response.status_code, elapsed_ms)
        if response.status_code >= 400:
            raise _error_from_response(
                response.status_code,
                response.headers,
                response.content,
                inference=path.startswith("/v1/"),
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayUnavailable(
                response.status_code, "invalid_response", "The gateway sent an unreadable reply"
            ) from exc

    # -- users ------------------------------------------------------------

    async def _user_info(self, user_id: str) -> dict[str, Any] | None:
        try:
            data = await self.request("GET", "/user/info", params={"user_id": user_id})
        except GatewayNotFound:
            return None
        return _as_dict(_as_dict(data).get("user_info"))

    async def _create_user(self, user_id: str, wanted: Mapping[str, Any]) -> dict[str, Any]:
        """Create a gateway user and return the fields it now has.

        The gateway answers 409 both when the id exists (a concurrent creator
        won the race) and when another gateway user already has the email. In
        the second case the user is created without an email; ``ensure_user``
        then sets it through the update route, which has no such restriction.
        """
        body = {"user_id": user_id, "auto_create_key": False, **wanted}
        try:
            await self.request("POST", "/user/new", json=body)
        except GatewayError as exc:
            if exc.status != 409:
                raise
            existing = await self._user_info(user_id)
            if existing is not None:
                return existing
            without_email = {name: value for name, value in body.items() if name != "user_email"}
            await self.request("POST", "/user/new", json=without_email)
            return {name: value for name, value in wanted.items() if name != "user_email"}
        return dict(wanted)

    async def ensure_user(self, user_id: str, email: str, rpm_limit: int, tpm_limit: int) -> None:
        """Make sure the gateway user exists with this email and these limits. Idempotent."""
        wanted = {
            "user_email": email,
            "user_role": "internal_user",
            "rpm_limit": rpm_limit,
            "tpm_limit": tpm_limit,
        }
        current = await self._user_info(user_id)
        if current is None:
            current = await self._create_user(user_id, wanted)
        if any(current.get(name) != value for name, value in wanted.items()):
            await self.request("POST", "/user/update", json={"user_id": user_id, **wanted})

    async def update_user_limits(self, user_id: str, rpm_limit: int, tpm_limit: int) -> None:
        """Change a user's limits across all of their keys.

        Raises ``GatewayNotFound`` for an unknown user. The existence check is
        needed because the gateway's own update route silently creates users.
        """
        if await self._user_info(user_id) is None:
            raise GatewayNotFound(404, "user_not_found", f"User {user_id} not found")
        await self.request(
            "POST",
            "/user/update",
            json={"user_id": user_id, "rpm_limit": rpm_limit, "tpm_limit": tpm_limit},
        )

    async def delete_user(self, user_id: str) -> None:
        """Delete a user and, with it, all of their keys. Unknown users are fine."""
        try:
            await self.request("POST", "/user/delete", json={"user_ids": [user_id]})
        except GatewayNotFound:
            return

    # -- keys -------------------------------------------------------------

    async def create_key(
        self, user_id: str, name: str, kind: KeyKind, rpm_limit: int, tpm_limit: int
    ) -> NewKey:
        """Create a key that can only call the model. The secret is in the result, once.

        Raises ``GatewayNotFound`` if the gateway user does not exist yet. The
        gateway itself would happily create the key anyway, but a key without a
        user escapes the per-user limits, so provision the user first
        (``UserService.ensure_provisioned`` does).
        """
        if await self._user_info(user_id) is None:
            raise GatewayNotFound(404, "user_not_found", f"User {user_id} not found")
        body = {
            "user_id": user_id,
            "models": [self.model],
            "key_type": "llm_api",
            "rpm_limit": rpm_limit,
            "tpm_limit": tpm_limit,
            "metadata": {"portal": {"name": name, "kind": kind}},
        }
        attempts_left = ALIAS_ATTEMPTS
        while True:
            attempts_left -= 1
            alias = key_alias(kind, user_id, name)
            try:
                data = await self.request(
                    "POST", "/key/generate", json={**body, "key_alias": alias}
                )
            except GatewayError as exc:
                alias_taken = exc.status == 400 and "already exists" in exc.message
                if not (alias_taken and attempts_left):
                    raise
                continue
            if not (_as_dict(data).get("key") and _as_dict(data).get("token")):
                raise GatewayUnavailable(200, "invalid_response", "The gateway returned no key")
            return NewKey.from_gateway(data)

    async def list_keys(self, user_id: str) -> list[KeyInfo]:
        """All of a user's keys, newest first."""
        keys: list[KeyInfo] = []
        for page in range(1, MAX_PAGES + 1):
            data = _as_dict(
                await self.request(
                    "GET",
                    "/key/list",
                    params={
                        "user_id": user_id,
                        "return_full_object": "true",
                        "page": page,
                        "size": PAGE_SIZE,
                    },
                )
            )
            rows = [row for row in data.get("keys") or [] if isinstance(row, dict)]
            keys.extend(KeyInfo.from_gateway(row) for row in rows if row.get("user_id") == user_id)
            if not rows or page >= _as_int(data.get("total_pages")):
                break
        epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
        return sorted(keys, key=lambda key: key.created_at or epoch, reverse=True)

    async def _key_info(self, token: str) -> dict[str, Any] | None:
        """The gateway's record of a live key, or ``None`` if there is no such key.

        The gateway keeps a record of every key revoked through ``/key/delete``
        and still answers 200 for it here, marked ``status: deleted``. Such a
        key cannot be used or changed any more, so it counts as gone.
        """
        try:
            data = await self.request("GET", "/key/info", params={"key": token})
        except GatewayNotFound:
            return None
        info = _as_dict(_as_dict(data).get("info"))
        if info.get("status") == "deleted" or info.get("deleted_at"):
            return None
        return info

    async def get_key(self, token: str) -> KeyInfo | None:
        """Look one key up by its token. ``None`` if the gateway does not know it."""
        info = await self._key_info(token)
        return None if info is None else KeyInfo.from_gateway(info, token=token)

    async def delete_key(self, token: str) -> None:
        """Revoke a key for good. Already-deleted keys are fine."""
        try:
            await self.request("POST", "/key/delete", json={"keys": [token]})
        except GatewayNotFound:
            return

    async def block_key(self, token: str) -> None:
        """Suspend a key: calls made with it get 401 until it is unblocked."""
        await self.request("POST", "/key/block", json={"key": token})

    async def unblock_key(self, token: str) -> None:
        await self.request("POST", "/key/unblock", json={"key": token})

    async def rename_key(self, token: str, name: str) -> None:
        """Change the display name. The alias and everything else stay as they are."""
        info = await self._key_info(token)
        if info is None:
            raise GatewayNotFound(404, "key_not_found", "Key not found")
        metadata = _as_dict(info.get("metadata"))
        portal = {**_as_dict(metadata.get("portal")), "name": name}
        await self.request(
            "POST", "/key/update", json={"key": token, "metadata": {**metadata, "portal": portal}}
        )

    async def update_key_limits(self, token: str, rpm_limit: int, tpm_limit: int) -> None:
        await self.request(
            "POST",
            "/key/update",
            json={"key": token, "rpm_limit": rpm_limit, "tpm_limit": tpm_limit},
        )

    # -- usage ------------------------------------------------------------

    async def daily_usage(
        self, user_id: str | None, start: dt.date, end: dt.date
    ) -> list[DayUsage]:
        """Usage per UTC day from ``start`` to ``end`` inclusive, oldest first.

        Every day in the range is present, zero-filled when there was no
        activity. ``user_id=None`` adds up all users. The gateway lags by
        roughly 15 seconds.
        """
        if start > end:
            raise ValueError("start must not be after end")
        params: dict[str, Any] = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "page_size": ACTIVITY_PAGE_SIZE,
        }
        if user_id is not None:
            params["user_id"] = user_id
        # One day can be spread over several pages; collect its rows first.
        rows_by_day: dict[dt.date, list[Mapping[str, Any]]] = {}
        for page in range(1, MAX_PAGES + 1):
            data = _as_dict(
                await self.request("GET", "/user/daily/activity", params={**params, "page": page})
            )
            for row in data.get("results") or []:
                day = _parse_date(_as_dict(row).get("date"))
                if day is not None and start <= day <= end:
                    rows_by_day.setdefault(day, []).append(_as_dict(row))
            if not _as_dict(data.get("metadata")).get("has_more"):
                break
        return [
            DayUsage.from_gateway(day, rows_by_day[day])
            if day in rows_by_day
            else DayUsage.empty(day)
            for day in _days(start, end)
        ]

    async def usage_by_user(self, start: dt.date, end: dt.date) -> list[UserUsage]:
        """Totals per gateway user over the range, biggest consumer first."""
        users: list[UserUsage] = []
        for page in range(MAX_PAGES):
            data = _as_dict(
                await self.request(
                    "GET",
                    "/user/daily/activity/aggregated/users",
                    params={
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "offset": page * PAGE_SIZE,
                        "limit": PAGE_SIZE,
                    },
                )
            )
            rows = data.get("users") or []
            users.extend(UserUsage.from_gateway(_as_dict(row)) for row in rows)
            if not rows or len(users) >= _as_int(data.get("total_users")):
                break
        return sorted(users, key=lambda user: (-user.total_tokens, -user.requests, user.user_id))

    async def recent_requests(
        self,
        user_id: str | None,
        page: int = 1,
        page_size: int = 20,
        *,
        days: int = REQUEST_LOG_DAYS,
    ) -> tuple[list[RequestLog], int]:
        """One page of the request log, newest first, and the total number of rows.

        Looks back ``days`` days. ``user_id=None`` returns every user's requests.
        """
        now = dt.datetime.now(dt.UTC)
        stamp = "%Y-%m-%d %H:%M:%S"
        params: dict[str, Any] = {
            "start_date": (now - dt.timedelta(days=days)).strftime(stamp),
            "end_date": (now + dt.timedelta(days=1)).strftime(stamp),
            "page": page,
            "page_size": page_size,
        }
        if user_id is not None:
            params["user_id"] = user_id
        data = _as_dict(await self.request("GET", "/spend/logs/v2", params=params))
        rows = [RequestLog.from_gateway(row) for row in data.get("data") or []]
        return rows, _as_int(data.get("total"))

    # -- health and models --------------------------------------------------

    async def ready(self) -> bool:
        """True if the gateway process is up and connected to its database."""
        try:
            data = await self.request("GET", "/health/readiness", timeout=READINESS_TIMEOUT)
        except GatewayError:
            return False
        return _as_dict(data).get("status") == "healthy"

    async def health(self) -> ModelHealth:
        """Ask the gateway to make a tiny real call to the model.

        ``online`` when at least one deployment answers; ``offline`` when none
        does (the gateway then answers 503) or the check hangs (the gateway can
        sit on a dead upstream for a minute, hence the short timeout);
        ``unknown`` when the gateway itself cannot be asked.
        """
        started = time.perf_counter()
        try:
            data = _as_dict(
                await self.request(
                    "GET", "/health", params={"model": self.model}, timeout=HEALTH_TIMEOUT
                )
            )
        except GatewayError as exc:
            # With no healthy deployment the gateway answers /health itself, with a 503:
            # it is reachable and the model is not.
            model_down = exc.code == "gateway_timeout" or exc.status == 503
            state: HealthState = "offline" if model_down else "unknown"
            return ModelHealth(state, None, dt.datetime.now(dt.UTC), exc.message)
        return ModelHealth.from_gateway(data, round((time.perf_counter() - started) * 1000))

    async def list_models(self, api_key: str | None = None) -> list[str]:
        """Model ids visible to ``api_key`` (or to the master key)."""
        data = _as_dict(await self.request("GET", "/v1/models", api_key=api_key))
        return [str(model["id"]) for model in data.get("data") or [] if _as_dict(model).get("id")]

    # -- inference ----------------------------------------------------------

    async def chat_stream(
        self, api_key: str, payload: Mapping[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion, yielding each parsed chunk until ``[DONE]``.

        ``payload`` is an OpenAI chat request; streaming and the final usage
        chunk are switched on here and the model defaults to the portal's.
        Chunks carry ``choices[0].delta`` with ``reasoning_content`` pieces
        first, then ``content`` pieces, then a chunk with ``finish_reason``,
        and last a chunk with empty ``choices`` and the ``usage`` totals.

        Raises ``GatewayError`` when the gateway answers anything but 200
        (inspect ``.kind``) or reports an error mid-stream, and
        ``GatewayUnavailable`` when the connection fails.

        To stop early, close the generator; that closes the upstream connection
        at once so the model stops generating::

            async with contextlib.aclosing(gateway.chat_stream(key, payload)) as chunks:
                async for chunk in chunks:
                    ...
        """
        body = {"model": self.model, **payload, "stream": True}
        body["stream_options"] = {**_as_dict(payload.get("stream_options")), "include_usage": True}
        timeout = httpx.Timeout(STREAM_READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        try:
            async with self._http.stream(
                "POST",
                self._url("/v1/chat/completions"),
                json=body,
                headers=self._headers(api_key),
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    raise _error_from_response(
                        response.status_code,
                        response.headers,
                        await response.aread(),
                        inference=True,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    chunk = _decode_chunk(data)
                    if chunk is None:
                        continue
                    if "error" in chunk and not chunk.get("choices"):
                        raise _stream_error(chunk)
                    yield chunk
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc


def _decode_chunk(data: str) -> dict[str, Any] | None:
    """Parse one SSE data payload. Anything that is not a JSON object is skipped."""
    try:
        chunk = json.loads(data)
    except ValueError:
        logger.warning("Skipping a malformed chunk in the chat stream")
        return None
    return chunk if isinstance(chunk, dict) else None
