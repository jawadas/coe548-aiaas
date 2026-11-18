"""Database access: the connection pool, the migration runner and a few helpers.

The portal talks to its own database ("portal") with hand-written SQL through
asyncpg. Migrations are plain ``.sql`` files in ``app/migrations``; any file
that has not been recorded in ``schema_migrations`` is applied at start-up, in
filename order, so adding ``010_something.sql`` is all a feature has to do.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Advisory lock key shared by every portal process, so only one migrates at a time.
MIGRATION_LOCK_ID = 5_480_005
MIGRATION_TIMEOUT = 600.0

CONNECT_RETRY_SECONDS = 30.0
CONNECT_RETRY_DELAY = 1.0
CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10

# Failures that mean "the database cannot be reached right now" rather than a bug
# in a query. InterfaceError is what asyncpg raises once the pool is closed or closing.
UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    asyncpg.PostgresConnectionError,
    asyncpg.OperatorInterventionError,
    asyncpg.TooManyConnectionsError,
    asyncpg.InterfaceError,
    ConnectionError,
    socket.gaierror,
    TimeoutError,
)

type Executor = asyncpg.Pool | asyncpg.Connection

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Send and receive json/jsonb columns as Python objects."""
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def create_pool(
    dsn: str,
    *,
    retry_seconds: float = CONNECT_RETRY_SECONDS,
    max_size: int = POOL_MAX_SIZE,
) -> asyncpg.Pool:
    """Open the connection pool, waiting up to ``retry_seconds`` for Postgres to be ready.

    During a ``docker compose up`` the database may still be starting, or its
    role and database may still be on their way, so every connection failure is
    retried until the deadline. The DSN (which holds the password) is never logged.
    """
    deadline = time.monotonic() + retry_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncpg.create_pool(
                dsn,
                min_size=POOL_MIN_SIZE,
                max_size=max_size,
                init=_init_connection,
                timeout=CONNECT_TIMEOUT,
                command_timeout=COMMAND_TIMEOUT,
            )
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            if time.monotonic() >= deadline:
                raise
            logger.warning(
                "Database not ready (attempt %d): %s: %s", attempt, type(exc).__name__, exc
            )
            await asyncio.sleep(CONNECT_RETRY_DELAY)


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files in the order they must be applied."""
    return sorted(directory.glob("*.sql"), key=lambda path: path.name)


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration that has not been recorded yet. Returns the names applied.

    Safe to call on every start and from several processes at once: a session
    advisory lock serialises the runners, and each file is applied and recorded
    in one transaction, so a failed migration leaves no trace.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID, timeout=MIGRATION_TIMEOUT
        )
        try:
            return await _apply_pending(conn, directory)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)


async def _apply_pending(conn: asyncpg.Connection, directory: Path) -> list[str]:
    await conn.execute(_CREATE_MIGRATIONS_TABLE)
    rows = await conn.fetch("SELECT filename, checksum FROM schema_migrations")
    recorded = {row["filename"]: row["checksum"] for row in rows}
    applied = []
    for path in migration_files(directory):
        sql = path.read_text(encoding="utf-8")
        if path.name in recorded:
            if recorded[path.name] != _checksum(sql):
                logger.warning(
                    "Migration %s was edited after it was applied; the edit is ignored. "
                    "Add a new migration file instead.",
                    path.name,
                )
            continue
        async with conn.transaction():
            await conn.execute(sql, timeout=MIGRATION_TIMEOUT)
            await conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                path.name,
                _checksum(sql),
            )
        logger.info("Applied migration %s", path.name)
        applied.append(path.name)
    return applied


async def pending_migrations(executor: Executor, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Names of the migration files that have not been applied to this database."""
    names = [path.name for path in migration_files(directory)]
    if await executor.fetchval("SELECT to_regclass('schema_migrations')") is None:
        return names
    rows = await executor.fetch("SELECT filename FROM schema_migrations")
    recorded = {row["filename"] for row in rows}
    return [name for name in names if name not in recorded]


async def ping(pool: asyncpg.Pool, seconds: float = 2.0) -> bool:
    """True if the database answers a trivial query within ``seconds``."""
    try:
        async with asyncio.timeout(seconds):
            return await pool.fetchval("SELECT 1") == 1
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def audit(
    executor: Executor,
    action: str,
    *,
    actor_user_id: UUID | None = None,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Append one row to the audit log.

    ``action`` is a dotted verb such as ``user.login`` or ``key.created``;
    ``target`` names what was acted on (a user id, a key token). Never put
    secrets in ``detail``.
    """
    await executor.execute(
        "INSERT INTO audit_log (actor_user_id, action, target, detail, ip) "
        "VALUES ($1, $2, $3, $4, $5)",
        actor_user_id,
        action,
        target,
        detail,
        ip,
    )
