"""Operator commands. Run inside the container: ``python -m app.cli <command>``.

  selfcheck              check the database, the gateway and the model end to end
  list-users             every account with its role and status
  promote <email>        make the account an admin
  demote <email>         make the account a regular user
  disable <email>        sign the user out, block their API keys, refuse sign-in
  enable <email>         undo ``disable``
  set-password <email>   set a new password (asked for twice, never echoed)

``selfcheck`` prints one line per check, ``PASS``, ``WARN`` or ``FAIL``, and
exits non-zero if anything failed. The model being offline is only a warning:
the GPU pod runs on demand. No command prints a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import secrets
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import asyncpg

from app import db
from app.config import ConfigError, Settings, get_settings
from app.gateway import GatewayClient, GatewayError
from app.users import UserInputError, UserService

DATABASE_WAIT_SECONDS = 5.0
SELFCHECK_RPM = 5
SELFCHECK_TPM = 1000
LIST_PAGE_SIZE = 200


class Report:
    """Prints each check as it finishes and remembers whether any failed."""

    def __init__(self) -> None:
        self.failed = False

    def record(self, outcome: str, check: str, detail: str) -> None:
        self.failed = self.failed or outcome == "FAIL"
        print(f"{outcome:<4}  {check:<18} {detail}")


def _gateway_client(settings: Settings) -> GatewayClient:
    return GatewayClient(
        settings.litellm_base_url, settings.litellm_master_key.get_secret_value(), settings.model
    )


async def _open_pool(settings: Settings) -> asyncpg.Pool:
    return await db.create_pool(
        settings.database_url.get_secret_value(), retry_seconds=DATABASE_WAIT_SECONDS, max_size=2
    )


# -- selfcheck -----------------------------------------------------------------


async def _write_rolls_back(pool: asyncpg.Pool) -> bool:
    """Insert a row inside a transaction, see it, roll back, and see it gone."""
    marker = secrets.token_hex(8)
    count = "SELECT count(*) FROM audit_log WHERE action = 'selfcheck' AND target = $1"
    async with pool.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "INSERT INTO audit_log (action, target) VALUES ('selfcheck', $1)", marker
            )
            seen_inside = await connection.fetchval(count, marker)
        finally:
            await transaction.rollback()
        return seen_inside == 1 and await connection.fetchval(count, marker) == 0


async def _check_database(report: Report, settings: Settings) -> None:
    try:
        pool = await _open_pool(settings)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        report.record("FAIL", "database", f"cannot connect: {type(exc).__name__}")
        return
    try:
        report.record("PASS", "database", "connected")
        pending = await db.pending_migrations(pool)
        if pending:
            report.record("FAIL", "migrations", "not applied: " + ", ".join(pending))
            return
        report.record("PASS", "migrations", f"all {len(db.migration_files())} applied")
        if await _write_rolls_back(pool):
            report.record("PASS", "database write", "insert visible, then rolled back")
        else:
            report.record("FAIL", "database write", "a rolled-back insert left a trace")
    except asyncpg.PostgresError as exc:
        report.record("FAIL", "database", f"{type(exc).__name__}: {exc}")
    finally:
        await pool.close()


async def _admin_round_trip(gateway: GatewayClient, user_id: str) -> str | None:
    """Create, list and delete a temporary user and key. Returns what went wrong, if anything."""
    await gateway.ensure_user(user_id, f"{user_id}@selfcheck.invalid", SELFCHECK_RPM, SELFCHECK_TPM)
    key = await gateway.create_key(user_id, "selfcheck", "api", SELFCHECK_RPM, SELFCHECK_TPM)
    if [listed.token for listed in await gateway.list_keys(user_id)] != [key.info.token]:
        return "the new key is missing from the user's key list"
    await gateway.delete_key(key.info.token)
    await gateway.delete_user(user_id)
    if await gateway.get_key(key.info.token) is not None:
        return "the key still exists after it was deleted"
    return None


async def _check_gateway(report: Report, gateway: GatewayClient) -> None:
    if not await gateway.ready():
        report.record("FAIL", "gateway", "not reachable or not ready")
        return
    report.record("PASS", "gateway", "ready")
    user_id = f"portal-selfcheck-{secrets.token_hex(4)}"
    try:
        problem = await _admin_round_trip(gateway, user_id)
    except GatewayError as exc:
        problem = f"{exc.code} (HTTP {exc.status})"
    finally:
        with contextlib.suppress(GatewayError):
            await gateway.delete_user(user_id)
    if problem:
        report.record("FAIL", "gateway admin API", problem)
    else:
        report.record("PASS", "gateway admin API", "created, listed and deleted a user and a key")


async def _check_model(report: Report, gateway: GatewayClient) -> None:
    health = await gateway.health()
    if health.is_online:
        report.record("PASS", "model", f"online ({health.latency_ms} ms)")
    else:
        # The reason is left out on purpose: it can name the upstream GPU host.
        report.record("WARN", "model", f"{health.state} (is the GPU pod running?)")


async def selfcheck(settings: Settings, gateway: GatewayClient | None = None) -> int:
    """Run every check. ``gateway`` lets tests substitute a fake."""
    report = Report()
    await _check_database(report, settings)
    client = gateway or _gateway_client(settings)
    try:
        await _check_gateway(report, client)
        await _check_model(report, client)
    finally:
        if gateway is None:
            await client.aclose()
    print("selfcheck failed" if report.failed else "selfcheck passed")
    return 1 if report.failed else 0


# -- account commands ------------------------------------------------------------


@asynccontextmanager
async def _user_service(
    settings: Settings, gateway: GatewayClient | None
) -> AsyncIterator[UserService]:
    pool = await _open_pool(settings)
    client = gateway or _gateway_client(settings)
    try:
        yield UserService(pool, client, settings)
    finally:
        if gateway is None:
            await client.aclose()
        await pool.close()


async def list_users(settings: Settings, gateway: GatewayClient | None = None) -> int:
    async with _user_service(settings, gateway) as users:
        print(f"{'EMAIL':<40} {'ROLE':<6} {'STATUS':<9} {'CREATED':<11} NAME")
        offset = 0
        while page := await users.list_users(limit=LIST_PAGE_SIZE, offset=offset):
            for user in page:
                print(
                    f"{user.email:<40} {user.role:<6} {user.status:<9} "
                    f"{user.created_at:%Y-%m-%d}  {user.name}"
                )
            offset += len(page)
        print(f"{offset} account(s)")
    return 0


def _read_new_password() -> str | None:
    password = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != password:
        print("The two entries do not match.", file=sys.stderr)
        return None
    return password


async def change_user(
    settings: Settings, command: str, email: str, gateway: GatewayClient | None = None
) -> int:
    """Run ``promote``, ``demote``, ``disable``, ``enable`` or ``set-password`` for one account."""
    async with _user_service(settings, gateway) as users:
        user = await users.get_by_email(email)
        if user is None:
            print(f"No account with the email {email}.", file=sys.stderr)
            return 1
        try:
            if command == "promote":
                await users.set_role(user, "admin")
                print(f"{user.email} is now an admin.")
            elif command == "demote":
                await users.set_role(user, "user")
                print(f"{user.email} is now a regular user.")
            elif command == "disable":
                await users.set_status(user, "disabled")
                print(f"{user.email} is disabled: signed out everywhere, API keys blocked.")
            elif command == "enable":
                await users.set_status(user, "active")
                print(f"{user.email} is active again.")
            else:
                password = _read_new_password()
                if password is None:
                    return 1
                await users.set_password(user, password)
                print(f"Password changed for {user.email}; they are signed out everywhere.")
        except UserInputError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except GatewayError as exc:
            print(
                f"The gateway refused or is unavailable ({exc.code}). Try again.", file=sys.stderr
            )
            return 1
    return 0


# -- entry point -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Portal operator tools.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("selfcheck", help="check the database, the gateway and the model")
    commands.add_parser("list-users", help="list every account")
    for name, text in (
        ("promote", "make an account an admin"),
        ("demote", "make an account a regular user"),
        ("disable", "sign a user out, block their keys and refuse sign-in"),
        ("enable", "undo disable"),
        ("set-password", "set a new password for an account"),
    ):
        commands.add_parser(name, help=text).add_argument("email")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "selfcheck":
        return asyncio.run(selfcheck(settings))
    try:
        if args.command == "list-users":
            return asyncio.run(list_users(settings))
        return asyncio.run(change_user(settings, args.command, args.email))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        print(f"Cannot use the database: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
