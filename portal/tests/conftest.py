"""Shared fixtures.

The database is real: every test session creates a throwaway database on the
local Postgres (the one of the development stack), migrates it, and drops it at
the end. Tables are emptied before each test. The gateway is ``FakeGateway``
unless a test is marked ``integration``.

Where the Postgres is: 127.0.0.1:55432, user ``aiaas``, password from
``POSTGRES_PASSWORD`` in the repository's ``.env``. Override with
``TEST_PG_HOST``, ``TEST_PG_PORT``, ``TEST_PG_USER`` and ``TEST_PG_PASSWORD``.

The fixtures, from the outside in:

``client``         an anonymous browser (``httpx.AsyncClient``) on the application
``user_client``    a browser signed in as a regular user (``helpers.USER_EMAIL``)
``admin_client``   a browser signed in as an admin (``helpers.ADMIN_EMAIL``)
``app``            the started application, built from ``settings`` and ``gateway``
``app_factory``    builds more applications: ``await app_factory(open_signup=False)``
``gateway``        the ``FakeGateway`` behind ``app``
``settings``       the test ``Settings``; ``make_settings(url, **overrides)`` builds variants
``pool``           the asyncpg pool on the test database, with every table emptied
``scratch_database``  creates extra, empty databases (for migration tests)

The test settings differ from production in ways that keep tests short: open
sign-up, no first-user-admin rule, no auth rate limit, plain-HTTP cookies.
Tests of those rules switch them back on through ``app_factory``.

All tests and fixtures share one event loop (see ``pyproject.toml``).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app import db
from app.config import Settings
from app.main import create_app
from tests import helpers
from tests.fakes import FakeGateway

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_SECRET_KEY = "k3-test-only-portal-signing-key-9f2c7a1d5b8e4c60"
TEST_MASTER_KEY = "sk-test-master-key"
_SETTINGS_PREFIXES = ("PORTAL_", "LITELLM_")

type AppFactory = Callable[..., Awaitable[FastAPI]]
type DatabaseFactory = Callable[[], Awaitable[str]]


def repo_env() -> dict[str, str]:
    """The ``KEY=value`` pairs of the repository's ``.env`` (empty if there is none)."""
    path = REPO_ROOT / ".env"
    if not path.is_file():
        return {}
    pairs = (
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {name.strip(): value.strip().strip("'\"") for name, value in pairs}


def _postgres() -> dict[str, Any]:
    """How to reach the test Postgres as a role that may create databases."""
    return {
        "host": os.environ.get("TEST_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("TEST_PG_PORT", "55432")),
        "user": os.environ.get("TEST_PG_USER", "aiaas"),
        "password": os.environ.get("TEST_PG_PASSWORD") or repo_env().get("POSTGRES_PASSWORD", ""),
    }


def _database_url(name: str) -> str:
    server = _postgres()
    return (
        f"postgresql://{quote(server['user'], safe='')}:{quote(server['password'], safe='')}"
        f"@{server['host']}:{server['port']}/{name}"
    )


async def _admin_connection() -> asyncpg.Connection:
    server = _postgres()
    try:
        return await asyncpg.connect(**server, database="postgres", timeout=5)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.fail(
            f"Cannot reach the test Postgres at {server['host']}:{server['port']} "
            f"({type(exc).__name__}). Start it with: docker compose -f docker-compose.yml "
            "-f docker-compose.dev.yml up -d postgres",
            pytrace=False,
        )


async def _create_database() -> str:
    """Create an empty database with a random name. Returns the name."""
    name = f"portal_test_{secrets.token_hex(6)}"
    admin = await _admin_connection()
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    finally:
        await admin.close()
    return name


async def _drop_database(name: str) -> None:
    admin = await _admin_connection()
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


def make_settings(database_url: str, **overrides: Any) -> Settings:
    """Test settings; ``overrides`` are ``Settings`` field names, e.g. ``open_signup=False``."""
    values: dict[str, Any] = {
        "env": "development",
        "public_url": "http://testserver",
        "database_url": database_url,
        "secret_key": TEST_SECRET_KEY,
        "litellm_base_url": "http://gateway.test:4000",
        "litellm_master_key": TEST_MASTER_KEY,
        "open_signup": True,
        "first_user_admin": False,
        "admin_emails": helpers.ADMIN_EMAIL,
        "cookie_secure": False,
        "auth_rate_limit": False,
    }
    return Settings(**{**values, **overrides})


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment() -> Iterator[None]:
    """Keep PORTAL_* and LITELLM_* variables of the developer's shell out of the tests."""
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith(_SETTINGS_PREFIXES):
                patch.delenv(name)
        yield


@pytest.fixture(scope="session")
async def database_url() -> AsyncIterator[str]:
    """URL of this session's throwaway database (not yet migrated)."""
    name = await _create_database()
    try:
        yield _database_url(name)
    finally:
        await _drop_database(name)


@pytest.fixture(scope="session")
async def migrated_pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """The session-wide pool, opened once the migrations have run."""
    pool = await db.create_pool(database_url, retry_seconds=0)
    try:
        await db.migrate(pool)
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def pool(migrated_pool: asyncpg.Pool) -> asyncpg.Pool:
    """The test database with every table emptied (including ones later migrations add)."""
    tables = await migrated_pool.fetch(
        "SELECT quote_ident(tablename) AS name FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
    )
    names = ", ".join(row["name"] for row in tables)
    await migrated_pool.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    return migrated_pool


@pytest.fixture
async def scratch_database() -> AsyncIterator[DatabaseFactory]:
    """``await scratch_database()`` creates one more empty database and returns its URL."""
    created: list[str] = []

    async def create() -> str:
        created.append(await _create_database())
        return _database_url(created[-1])

    try:
        yield create
    finally:
        for name in created:
            await _drop_database(name)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return make_settings(database_url)


@pytest.fixture
def gateway(settings: Settings) -> FakeGateway:
    return FakeGateway(settings.model)


@pytest.fixture
async def app_factory(
    pool: asyncpg.Pool, database_url: str, gateway: FakeGateway
) -> AsyncIterator[AppFactory]:
    """Build and start applications on the test database.

    ``await app_factory(**overrides)`` uses the ``gateway`` fixture and the test
    settings with ``overrides`` applied; pass ``settings=`` or ``gateway=`` to
    replace either outright. Everything is shut down when the test ends.
    """
    async with AsyncExitStack() as stack:

        async def build(
            *, settings: Settings | None = None, gateway_: Any = None, **overrides: Any
        ) -> FastAPI:
            application = create_app(
                settings or make_settings(database_url, **overrides),
                gateway=gateway_ or gateway,
                pool=pool,
            )
            await stack.enter_async_context(application.router.lifespan_context(application))
            return application

        yield build


@pytest.fixture
async def app(app_factory: AppFactory, settings: Settings) -> FastAPI:
    """The started application with the default test settings and the fake gateway."""
    return await app_factory(settings=settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An anonymous browser."""
    async with helpers.new_client(app) as browser:
        yield browser


@pytest.fixture
async def user_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A browser signed in as a regular user, ``helpers.USER_EMAIL``."""
    async with helpers.new_client(app) as browser:
        response = await helpers.sign_up(browser, helpers.USER_EMAIL)
        assert response.status_code == 303, response.text
        yield browser


@pytest.fixture
async def admin_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A browser signed in as an admin, ``helpers.ADMIN_EMAIL``."""
    async with helpers.new_client(app) as browser:
        response = await helpers.sign_up(browser, helpers.ADMIN_EMAIL, name="Admin User")
        assert response.status_code == 303, response.text
        yield browser
