"""Server-side sessions.

The browser holds an opaque random token in a cookie; the database holds only
its SHA-256, so a copy of the ``sessions`` table cannot be replayed as cookies.
Each session also carries the CSRF token that its forms and scripts must echo.

Expiry is sliding: activity pushes ``expires_at`` forward by the configured
lifetime (PORTAL_SESSION_HOURS), written at most once every five minutes, and
never beyond thirty days after the session was created. All clock arithmetic
happens in the database, so the application and Postgres cannot disagree.

Every function takes an ``Executor`` (the pool or one connection) first.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Self
from uuid import UUID

import asyncpg
from starlette.responses import Response

from app import security
from app.config import Settings
from app.db import Executor

HARD_CAP = dt.timedelta(days=30)
TOUCH_INTERVAL = dt.timedelta(minutes=5)
MAX_USER_AGENT_LENGTH = 300


@dataclass(frozen=True, slots=True)
class Session:
    """One row of ``sessions``. ``token_hash`` identifies it; the cookie value is not kept."""

    token_hash: str
    user_id: UUID
    csrf_token: str = field(repr=False)
    created_at: dt.datetime
    last_seen_at: dt.datetime
    expires_at: dt.datetime
    ip: str | None
    user_agent: str | None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Self:
        return cls(**{name: row[name] for name in cls.__slots__})


async def create(
    executor: Executor,
    user_id: UUID,
    *,
    lifetime: dt.timedelta,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, Session]:
    """Start a session. Returns the cookie value (shown only here) and the stored session."""
    token = security.new_token()
    row = await executor.fetchrow(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at, ip, user_agent) "
        "VALUES ($1, $2, $3, now() + LEAST($4::interval, $5::interval), $6, $7) RETURNING *",
        security.sha256_hex(token),
        user_id,
        security.new_token(),
        lifetime,
        HARD_CAP,
        ip,
        user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
    )
    return token, Session.from_row(row)


async def get(executor: Executor, token: str, *, lifetime: dt.timedelta) -> Session | None:
    """The live session for a cookie value, or ``None`` if it is unknown or expired.

    Slides the expiry when the session was last seen more than five minutes ago.
    """
    token_hash = security.sha256_hex(token)
    row = await executor.fetchrow(
        "SELECT *, last_seen_at < now() - $2::interval AS due "
        "FROM sessions WHERE token_hash = $1 AND expires_at > now()",
        token_hash,
        TOUCH_INTERVAL,
    )
    if row is not None and row["due"]:
        row = await executor.fetchrow(
            "UPDATE sessions SET last_seen_at = now(), "
            "expires_at = LEAST(now() + $2::interval, created_at + $3::interval) "
            "WHERE token_hash = $1 AND expires_at > now() RETURNING *",
            token_hash,
            lifetime,
            HARD_CAP,
        )
    return None if row is None else Session.from_row(row)


async def delete(executor: Executor, token: str) -> None:
    """End the session behind a cookie value (sign out)."""
    await executor.execute("DELETE FROM sessions WHERE token_hash = $1", security.sha256_hex(token))


async def delete_for_user(executor: Executor, user_id: UUID, *, keep: Session | None = None) -> int:
    """End a user's sessions, optionally all but ``keep``. Returns how many were ended."""
    status = await executor.execute(
        "DELETE FROM sessions WHERE user_id = $1 AND token_hash IS DISTINCT FROM $2",
        user_id,
        keep.token_hash if keep else None,
    )
    return _affected(status)


async def count_for_user(executor: Executor, user_id: UUID) -> int:
    """How many live sessions (signed-in browsers) a user has."""
    return await executor.fetchval(
        "SELECT count(*) FROM sessions WHERE user_id = $1 AND expires_at > now()", user_id
    )


async def purge_expired(executor: Executor) -> int:
    """Delete expired rows. Cheap; run opportunistically (at start-up and on each sign-in)."""
    return _affected(await executor.execute("DELETE FROM sessions WHERE expires_at <= now()"))


def set_cookie(response: Response, settings: Settings, token: str) -> None:
    """Hand the session token to the browser.

    The cookie outlives the sliding expiry on purpose (it lasts as long as the
    hard cap): the database decides when a session is over, not the browser.
    """
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=int(HARD_CAP.total_seconds()),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    """Remove the session cookie from the browser."""
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _affected(status: str) -> int:
    """Row count from an asyncpg command status such as ``DELETE 3``."""
    return int(status.rsplit(" ", 1)[-1])
