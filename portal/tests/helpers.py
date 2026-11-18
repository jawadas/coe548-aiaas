"""What tests do over and over: read CSRF tokens, sign up, sign in, post forms.

Every function takes the ``httpx.AsyncClient`` to act with. A client is one
browser: it keeps its own cookies, so two clients are two visitors.

    response = await sign_up(client)                      # now signed in as USER_EMAIL
    response = await post_form(client, "/keys", {"name": "Laptop"})
    response = await client.post("/api/things", json={...}, headers=await csrf_headers(client))
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import FastAPI

from app.users import User

USER_EMAIL = "user@example.com"
ADMIN_EMAIL = "admin@example.com"  # listed in PORTAL_ADMIN_EMAILS by the test settings
PASSWORD = "correct horse battery staple"

_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]*)">')


def new_client(app: FastAPI, *, ip: str | None = None) -> httpx.AsyncClient:
    """A fresh browser for ``app``. Redirects are not followed, so tests can assert on them.

    ``ip`` sets ``X-Forwarded-For`` on every request, as Caddy does in production.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=app.state.settings.public_url,
        headers={"X-Forwarded-For": ip} if ip else None,
    )


def csrf_from_html(html: str) -> str:
    """The token in the page's ``<meta name="csrf-token">``."""
    match = _CSRF_META.search(html)
    assert match, "the page has no <meta name=csrf-token>"
    return match.group(1)


async def csrf_token(client: httpx.AsyncClient, path: str = "/") -> str:
    """The CSRF token this client must send: the session's, or the anonymous cookie's.

    Fetches ``path`` (any page will do), which also plants the ``aiaas_csrf``
    cookie for an anonymous client.
    """
    response = await client.get(path)
    assert response.status_code == 200, f"GET {path} answered {response.status_code}"
    return csrf_from_html(response.text)


async def csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    """Headers for a ``fetch``-style request: ``{"X-CSRF-Token": ...}``."""
    return {"X-CSRF-Token": await csrf_token(client)}


async def post_form(
    client: httpx.AsyncClient, url: str, data: dict[str, Any] | None = None, **kwargs: Any
) -> httpx.Response:
    """POST a form the way a page would, with the CSRF token filled in."""
    token = await csrf_token(client)
    return await client.post(url, data={**(data or {}), "csrf_token": token}, **kwargs)


async def sign_up(
    client: httpx.AsyncClient,
    email: str = USER_EMAIL,
    *,
    name: str = "Test User",
    password: str = PASSWORD,
    invite_code: str | None = None,
) -> httpx.Response:
    """Submit the sign-up form. On success (303) the client is signed in."""
    data = {"name": name, "email": email, "password": password}
    if invite_code is not None:
        data["invite_code"] = invite_code
    return await post_form(client, "/signup", data)


async def log_in(
    client: httpx.AsyncClient,
    email: str = USER_EMAIL,
    password: str = PASSWORD,
    *,
    next_path: str | None = None,
) -> httpx.Response:
    """Submit the sign-in form. On success (303) the client is signed in."""
    data = {"email": email, "password": password}
    if next_path is not None:
        data["next"] = next_path
    return await post_form(client, "/login", data)


async def log_out(client: httpx.AsyncClient) -> httpx.Response:
    """Press "Sign out"."""
    return await post_form(client, "/logout")


async def user_row(app: FastAPI, email: str = USER_EMAIL) -> User:
    """The account with this email, read fresh from the database."""
    user = await app.state.users.get_by_email(email)
    assert user is not None, f"no account for {email}"
    return user


async def insert_user(pool: asyncpg.Pool, email: str = USER_EMAIL, **columns: Any) -> UUID:
    """Insert a bare ``users`` row without going through sign-up. Returns its id.

    For tests of the layers below the user service (sessions, the schema).
    The password hash is a placeholder that matches no password.
    """
    values = {
        "email": email,
        "name": "Test User",
        "password_hash": "!",
        "gateway_user_id": f"portal-test-{email}",
        **columns,
    }
    names = ", ".join(values)
    slots = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    return await pool.fetchval(
        f"INSERT INTO users ({names}) VALUES ({slots}) RETURNING id",  # noqa: S608 (test-only names)
        *values.values(),
    )
