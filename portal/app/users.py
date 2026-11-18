"""Portal accounts: the users table, sign-up, authentication and gateway provisioning.

Routes work with the one ``UserService`` the application builds at start-up
(``deps.get_users`` / ``UsersDep``). The plain functions at the top validate
input and can be used on their own, for example to check a new password.

Every portal user has a twin on the gateway, ``portal-<user id>``, which owns
the user's API keys and carries the per-user rate limits. That twin is created
lazily and repaired on demand by ``ensure_provisioned``: sign-up tries once
and does not fail if the gateway is down; every feature that needs the gateway
calls it again first.

Audit rows written here (``audit_log.action``): ``user.signup``, ``user.login``,
``user.logout``, ``user.role_change``, ``user.status_change``,
``user.password_change``, ``user.profile_change``, ``user.limits_change`` and
``user.delete``. ``target`` is the user's id; ``detail`` never holds secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import logging
import re
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

import asyncpg

from app import db, security, sessions
from app.config import Settings
from app.db import Executor
from app.gateway import GatewayClient, GatewayError, GatewayNotFound
from app.sessions import Session

logger = logging.getLogger(__name__)

EMAIL_MAX_LENGTH = 254
NAME_MAX_LENGTH = 80
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
PLAYGROUND_KEY_NAME = "Playground"
# Advisory lock that serialises sign-ups, so "is this the first account?" has one answer.
SIGNUP_LOCK_ID = 5_480_006

Role = Literal["user", "admin"]
Status = Literal["active", "disabled"]

_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")


class UserInputError(ValueError):
    """Submitted account data is not acceptable. ``errors`` maps field name to message."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class InvalidCredentials(Exception):
    """Unknown email or wrong password. Deliberately says nothing more."""


class AccountDisabled(Exception):
    """The account exists and the password was right, but an admin has disabled it."""


class UserNotFound(LookupError):
    """The user row is gone (deleted while the request was being handled)."""


def normalize_email(value: str) -> str:
    """Emails are stored and compared trimmed and lower-cased."""
    return value.strip().lower()


def normalize_name(value: str) -> str:
    """Trim a display name and collapse runs of whitespace."""
    return " ".join(value.split())


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def email_error(email: str) -> str | None:
    """Why a (normalised) email is not acceptable, or ``None`` if it is."""
    if not email:
        return "Enter your email address."
    if len(email) > EMAIL_MAX_LENGTH:
        return f"Email addresses can be at most {EMAIL_MAX_LENGTH} characters long."
    if _has_control_characters(email) or not _EMAIL_PATTERN.fullmatch(email):
        return "Enter a valid email address, like name@example.com."
    return None


def name_error(name: str) -> str | None:
    """Why a (normalised) display name is not acceptable, or ``None`` if it is."""
    if not name:
        return "Enter your name."
    if len(name) > NAME_MAX_LENGTH:
        return f"Names can be at most {NAME_MAX_LENGTH} characters long."
    if _has_control_characters(name):
        return "Names cannot contain control characters."
    return None


def password_error(password: str, email: str) -> str | None:
    """Why a password is not acceptable for the account with ``email``, or ``None`` if it is."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Use at most {PASSWORD_MAX_LENGTH} characters."
    if normalize_email(password) == normalize_email(email):
        return "Your password cannot be your email address."
    return None


@dataclass(frozen=True, slots=True)
class User:
    """One row of ``users``, without the password hash."""

    id: UUID
    email: str
    name: str
    role: str
    status: str
    gateway_user_id: str
    playground_key_enc: str | None = field(repr=False)
    playground_key_token: str | None
    rpm_limit: int | None
    tpm_limit: int | None
    max_keys: int | None
    created_at: dt.datetime
    last_login_at: dt.datetime | None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Self:
        return cls(**{name: row[name] for name in cls.__slots__})

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.strip() else self.name


@dataclass(frozen=True, slots=True)
class Limits:
    """A user's effective limits: their override where set, the platform default otherwise."""

    rpm_limit: int
    tpm_limit: int
    max_keys: int


@dataclass(frozen=True, slots=True)
class Provisioned:
    """What ``ensure_provisioned`` returns: the fresh user row and the playground key's secret."""

    user: User
    playground_key: str = field(repr=False)


async def fetch_user(executor: Executor, user_id: UUID) -> User | None:
    """Load one user through the pool or an open connection."""
    row = await executor.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return None if row is None else User.from_row(row)


def _search_pattern(search: str | None) -> str | None:
    """An ILIKE pattern matching ``search`` anywhere, with its wildcards taken literally."""
    text = (search or "").strip()
    if not text:
        return None
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class UserService:
    """Everything that reads or changes accounts, in the portal and on the gateway."""

    def __init__(self, pool: asyncpg.Pool, gateway: GatewayClient, settings: Settings) -> None:
        self._pool = pool
        self._gateway = gateway
        self._settings = settings
        self._box = security.SecretBox(settings.secret_key.get_secret_value())
        # One lock per user being provisioned; entries vanish once nobody holds the lock.
        self._provision_locks: weakref.WeakValueDictionary[UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    # -- reading ------------------------------------------------------------

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await fetch_user(self._pool, user_id)

    async def get_by_email(self, email: str) -> User | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM users WHERE lower(email) = $1", normalize_email(email)
        )
        return None if row is None else User.from_row(row)

    async def list_users(
        self,
        *,
        search: str | None = None,
        role: Role | None = None,
        status: Status | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[User]:
        """Users, newest first. ``search`` matches name or email, ignoring case."""
        rows = await self._pool.fetch(
            "SELECT * FROM users "
            "WHERE ($1::text IS NULL OR name ILIKE $1 OR email ILIKE $1) "
            "AND ($2::text IS NULL OR role = $2) AND ($3::text IS NULL OR status = $3) "
            "ORDER BY created_at DESC, id LIMIT $4 OFFSET $5",
            _search_pattern(search),
            role,
            status,
            limit,
            offset,
        )
        return [User.from_row(row) for row in rows]

    async def count_users(
        self,
        *,
        search: str | None = None,
        role: Role | None = None,
        status: Status | None = None,
    ) -> int:
        """How many users match the same filters as ``list_users``."""
        return await self._pool.fetchval(
            "SELECT count(*) FROM users "
            "WHERE ($1::text IS NULL OR name ILIKE $1 OR email ILIKE $1) "
            "AND ($2::text IS NULL OR role = $2) AND ($3::text IS NULL OR status = $3)",
            _search_pattern(search),
            role,
            status,
        )

    def limits_for(self, user: User) -> Limits:
        """The limits that apply to ``user`` right now."""
        settings = self._settings
        return Limits(
            rpm_limit=settings.user_rpm_limit if user.rpm_limit is None else user.rpm_limit,
            tpm_limit=settings.user_tpm_limit if user.tpm_limit is None else user.tpm_limit,
            max_keys=settings.max_keys_per_user if user.max_keys is None else user.max_keys,
        )

    # -- sign-up and sign-in --------------------------------------------------

    async def create_user(
        self, *, email: str, name: str, password: str, ip: str | None = None
    ) -> User:
        """Create an account and try to provision its gateway side.

        Raises ``UserInputError`` with per-field messages (fields ``name``,
        ``email``, ``password``) when the input is invalid or the email is
        taken. The new account is an admin if its email is listed in
        PORTAL_ADMIN_EMAILS, or if it is the first account ever created and
        PORTAL_FIRST_USER_ADMIN is on.
        """
        email, name = normalize_email(email), normalize_name(name)
        checks = {
            "name": name_error(name),
            "email": email_error(email),
            "password": password_error(password, email),
        }
        errors = {field_name: message for field_name, message in checks.items() if message}
        if errors:
            raise UserInputError(errors)
        password_hash = await security.hash_password(password)
        user_id = uuid.uuid4()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", SIGNUP_LOCK_ID)
            role = await self._role_for_new_account(conn, email)
            try:
                row = await conn.fetchrow(
                    "INSERT INTO users "
                    "(id, email, name, password_hash, role, gateway_user_id, last_login_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, now()) RETURNING *",
                    user_id,
                    email,
                    name,
                    password_hash,
                    role,
                    f"portal-{user_id}",
                )
            except asyncpg.UniqueViolationError:
                raise UserInputError(
                    {"email": "An account with this email already exists."}
                ) from None
            await db.audit(
                conn,
                "user.signup",
                actor_user_id=user_id,
                target=str(user_id),
                detail={"email": email, "role": role},
                ip=ip,
            )
        return await self._provision_quietly(User.from_row(row))

    async def _role_for_new_account(self, conn: asyncpg.Connection, email: str) -> Role:
        """Decide the role while holding the sign-up lock.

        "First ever" also looks at the audit trail, so emptying the users table
        does not hand admin rights to whoever signs up next.
        """
        if email in self._settings.admin_email_set:
            return "admin"
        if not self._settings.first_user_admin:
            return "user"
        seen_before = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users) "
            "OR EXISTS (SELECT 1 FROM audit_log WHERE action = 'user.signup')"
        )
        return "user" if seen_before else "admin"

    async def _provision_quietly(self, user: User) -> User:
        """Provision right after sign-up; a gateway problem must not fail the sign-up."""
        try:
            return (await self.ensure_provisioned(user)).user
        except GatewayError as exc:
            logger.warning(
                "Could not provision %s on the gateway (%s); it will be retried on first use",
                user.gateway_user_id,
                exc.code,
            )
            return user

    async def authenticate(self, email: str, password: str, *, ip: str | None = None) -> User:
        """Check a sign-in attempt and record it. Returns the user.

        Raises ``InvalidCredentials`` for an unknown email or a wrong password
        (after spending the same time either way) and ``AccountDisabled`` only
        when the password was right, so the difference reveals nothing to a
        stranger. Emails listed in PORTAL_ADMIN_EMAILS are promoted here.
        """
        row = await self._pool.fetchrow(
            "SELECT * FROM users WHERE lower(email) = $1", normalize_email(email)
        )
        if row is None or len(password) > PASSWORD_MAX_LENGTH:
            await security.dummy_verify(password[:PASSWORD_MAX_LENGTH])
            raise InvalidCredentials
        if not await security.verify_password(row["password_hash"], password):
            raise InvalidCredentials
        if row["status"] != "active":
            raise AccountDisabled
        return await self._complete_login(User.from_row(row), password, row["password_hash"], ip)

    async def _complete_login(
        self, user: User, password: str, stored_hash: str, ip: str | None
    ) -> User:
        promote = user.email in self._settings.admin_email_set and not user.is_admin
        # Re-hash transparently when argon2's recommended parameters have moved on.
        new_hash = (
            await security.hash_password(password)
            if security.password_needs_rehash(stored_hash)
            else None
        )
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE users SET last_login_at = now(), "
                "role = CASE WHEN $2::boolean THEN 'admin' ELSE role END, "
                "password_hash = COALESCE($3::text, password_hash) WHERE id = $1 RETURNING *",
                user.id,
                promote,
                new_hash,
            )
            if row is None:
                raise InvalidCredentials
            await db.audit(conn, "user.login", actor_user_id=user.id, target=str(user.id), ip=ip)
            if promote:
                await db.audit(
                    conn,
                    "user.role_change",
                    target=str(user.id),
                    detail={"role": "admin", "previous": user.role, "via": "PORTAL_ADMIN_EMAILS"},
                    ip=ip,
                )
        return User.from_row(row)

    async def check_password(self, user: User, password: str) -> bool:
        """True if ``password`` is the user's current password. No side effects."""
        stored = await self._pool.fetchval("SELECT password_hash FROM users WHERE id = $1", user.id)
        if stored is None or len(password) > PASSWORD_MAX_LENGTH:
            return False
        return await security.verify_password(stored, password)

    async def record_logout(self, user: User, *, ip: str | None = None) -> None:
        """Write the audit row for a sign-out (the caller ends the session)."""
        await db.audit(self._pool, "user.logout", actor_user_id=user.id, target=str(user.id), ip=ip)

    # -- gateway provisioning ---------------------------------------------------

    async def ensure_provisioned(
        self, user: User, *, reissue_playground_key: bool = False
    ) -> Provisioned:
        """Make sure the user's gateway side is complete, and return what it takes to use it.

        Idempotent and self-healing. After it returns, the gateway user exists
        with the user's effective limits, and a working playground key exists:
        present on the gateway, unblocked, with the configured limits, its
        secret readable with the current PORTAL_SECRET_KEY. Whatever is missing
        or broken is recreated. ``reissue_playground_key=True`` replaces the
        key even if it looks fine (use it once when the gateway rejects the key).

        Raises ``GatewayError`` (usually ``GatewayUnavailable``) when the
        gateway cannot do its part, ``AccountDisabled`` for a disabled account
        (its keys must stay blocked) and ``UserNotFound`` if the row is gone.
        """
        async with self._lock_for(user.id):
            # Read again under the lock: another request may have just provisioned.
            current = await self.get_by_id(user.id)
            if current is None:
                raise UserNotFound(str(user.id))
            if not current.is_active:
                raise AccountDisabled
            limits = self.limits_for(current)
            await self._gateway.ensure_user(
                current.gateway_user_id, current.email, limits.rpm_limit, limits.tpm_limit
            )
            secret = None if reissue_playground_key else await self._working_playground_key(current)
            if secret is None:
                current, secret = await self._issue_playground_key(current)
            return Provisioned(current, secret)

    def _lock_for(self, user_id: UUID) -> asyncio.Lock:
        lock = self._provision_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._provision_locks[user_id] = lock
        return lock

    async def _working_playground_key(self, user: User) -> str | None:
        """The stored playground secret if its key is usable, repairing what can be repaired."""
        if not user.playground_key_enc or not user.playground_key_token:
            return None
        secret = self._box.decrypt(user.playground_key_enc)
        if secret is None:
            return None
        info = await self._gateway.get_key(user.playground_key_token)
        if info is None or info.user_id != user.gateway_user_id:
            return None
        if info.blocked:
            await self._gateway.unblock_key(info.token)
        wanted = (self._settings.playground_rpm_limit, self._settings.key_tpm_limit)
        if (info.rpm_limit, info.tpm_limit) != wanted:
            await self._gateway.update_key_limits(info.token, *wanted)
        return secret

    async def _issue_playground_key(self, user: User) -> tuple[User, str]:
        """Create a fresh playground key, removing any earlier ones, and store it encrypted."""
        for key in await self._gateway.list_keys(user.gateway_user_id):
            if key.kind == "playground":
                await self._gateway.delete_key(key.token)
        new = await self._gateway.create_key(
            user.gateway_user_id,
            PLAYGROUND_KEY_NAME,
            "playground",
            self._settings.playground_rpm_limit,
            self._settings.key_tpm_limit,
        )
        row = await self._pool.fetchrow(
            "UPDATE users SET playground_key_enc = $2, playground_key_token = $3 "
            "WHERE id = $1 RETURNING *",
            user.id,
            self._box.encrypt(new.secret),
            new.info.token,
        )
        if row is None:
            await self._gateway.delete_key(new.info.token)
            raise UserNotFound(str(user.id))
        return User.from_row(row), new.secret

    # -- changes ------------------------------------------------------------------

    async def set_name(
        self, user: User, name: str, *, actor: User | None = None, ip: str | None = None
    ) -> User:
        """Change the display name. Raises ``UserInputError`` (field ``name``) if it is invalid."""
        name = normalize_name(name)
        if message := name_error(name):
            raise UserInputError({"name": message})
        return await self._update(
            "UPDATE users SET name = $2 WHERE id = $1 RETURNING *",
            user,
            name,
            action="user.profile_change",
            detail={"name": name},
            actor=actor,
            ip=ip,
        )

    async def set_role(
        self, user: User, role: Role, *, actor: User | None = None, ip: str | None = None
    ) -> User:
        """Make the user an admin or a regular user."""
        if role not in ("user", "admin"):
            raise ValueError(f"Unknown role: {role!r}")
        return await self._update(
            "UPDATE users SET role = $2 WHERE id = $1 RETURNING *",
            user,
            role,
            action="user.role_change",
            detail={"role": role, "previous": user.role},
            actor=actor,
            ip=ip,
        )

    async def set_password(
        self,
        user: User,
        password: str,
        *,
        keep: Session | None = None,
        actor: User | None = None,
        ip: str | None = None,
    ) -> User:
        """Set a new password and sign the user out everywhere except the ``keep`` session.

        Raises ``UserInputError`` (field ``password``) if the password breaks the rules.
        """
        if message := password_error(password, user.email):
            raise UserInputError({"password": message})
        password_hash = await security.hash_password(password)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE users SET password_hash = $2 WHERE id = $1 RETURNING *",
                user.id,
                password_hash,
            )
            if row is None:
                raise UserNotFound(str(user.id))
            await sessions.delete_for_user(conn, user.id, keep=keep)
            await self._audit(conn, "user.password_change", user, None, actor, ip)
        return User.from_row(row)

    async def set_limits(
        self,
        user: User,
        *,
        rpm_limit: int | None,
        tpm_limit: int | None,
        max_keys: int | None,
        actor: User | None = None,
        ip: str | None = None,
    ) -> User:
        """Store per-user overrides (``None`` = platform default) and apply them on the gateway.

        The gateway is updated first, so when it is unavailable the call raises
        and nothing is saved.
        """
        if (rpm_limit is not None and rpm_limit < 1) or (tpm_limit is not None and tpm_limit < 1):
            raise ValueError("Rate limits must be positive")
        if max_keys is not None and max_keys < 0:
            raise ValueError("max_keys cannot be negative")
        overrides = {"rpm_limit": rpm_limit, "tpm_limit": tpm_limit, "max_keys": max_keys}
        effective = self.limits_for(dataclasses.replace(user, **overrides))
        await self._gateway.ensure_user(
            user.gateway_user_id, user.email, effective.rpm_limit, effective.tpm_limit
        )
        return await self._update(
            "UPDATE users SET rpm_limit = $2, tpm_limit = $3, max_keys = $4 "
            "WHERE id = $1 RETURNING *",
            user,
            rpm_limit,
            tpm_limit,
            max_keys,
            action="user.limits_change",
            detail=overrides,
            actor=actor,
            ip=ip,
        )

    async def set_status(
        self, user: User, status: Status, *, actor: User | None = None, ip: str | None = None
    ) -> User:
        """Disable or re-enable an account.

        Disabling signs the user out everywhere and blocks each of their
        gateway keys that is not blocked already, remembering which; enabling
        unblocks exactly those, so keys the user had paused stay paused.

        If the gateway cannot be reached the call raises and nothing has
        changed. If it fails part-way through blocking, the account is already
        disabled and signed out; calling again finishes the job.
        """
        if status == "disabled":
            return await self._disable(user, actor, ip)
        if status == "active":
            return await self._enable(user, actor, ip)
        raise ValueError(f"Unknown status: {status!r}")

    async def _disable(self, user: User, actor: User | None, ip: str | None) -> User:
        keys = await self._gateway.list_keys(user.gateway_user_id)
        tokens = [key.token for key in keys if not key.blocked]
        async with self._pool.acquire() as conn, conn.transaction():
            # Record the tokens before blocking, so a failure in between loses nothing.
            row = await conn.fetchrow(
                "UPDATE users SET status = 'disabled', disabled_key_tokens = "
                "ARRAY(SELECT DISTINCT unnest(disabled_key_tokens || $2::text[])) "
                "WHERE id = $1 RETURNING *",
                user.id,
                tokens,
            )
            if row is None:
                raise UserNotFound(str(user.id))
            await sessions.delete_for_user(conn, user.id)
            await self._audit(conn, "user.status_change", user, {"status": "disabled"}, actor, ip)
        for token in tokens:
            with contextlib.suppress(GatewayNotFound):
                await self._gateway.block_key(token)
        return User.from_row(row)

    async def _enable(self, user: User, actor: User | None, ip: str | None) -> User:
        tokens = await self._pool.fetchval(
            "SELECT disabled_key_tokens FROM users WHERE id = $1", user.id
        )
        if tokens is None:
            raise UserNotFound(str(user.id))
        for token in tokens:
            with contextlib.suppress(GatewayNotFound):
                await self._gateway.unblock_key(token)
        return await self._update(
            "UPDATE users SET status = 'active', disabled_key_tokens = '{}' "
            "WHERE id = $1 RETURNING *",
            user,
            action="user.status_change",
            detail={"status": "active"},
            actor=actor,
            ip=ip,
        )

    async def delete_user(
        self, user: User, *, actor: User | None = None, ip: str | None = None
    ) -> None:
        """Delete the account for good: the gateway user and its keys first, then the row.

        The row's sessions and conversations go with it. Raises (and deletes
        nothing) when the gateway is unavailable, so no keys are left behind.
        """
        await self._gateway.delete_user(user.gateway_user_id)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM users WHERE id = $1", user.id)
            await self._audit(conn, "user.delete", user, None, actor, ip)

    async def _update(
        self,
        sql: str,
        user: User,
        *values: object,
        action: str,
        detail: dict[str, object] | None,
        actor: User | None,
        ip: str | None,
    ) -> User:
        """Run one ``UPDATE users ... WHERE id = $1 RETURNING *`` and audit it, atomically."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(sql, user.id, *values)
            if row is None:
                raise UserNotFound(str(user.id))
            await self._audit(conn, action, user, detail, actor, ip)
        return User.from_row(row)

    @staticmethod
    async def _audit(
        conn: asyncpg.Connection,
        action: str,
        user: User,
        detail: dict[str, object] | None,
        actor: User | None,
        ip: str | None,
    ) -> None:
        """Audit a change to ``user``. No ``actor`` means the system or an operator's CLI.

        The email goes into ``detail`` so the trail stays readable after the account is gone.
        """
        await db.audit(
            conn,
            action,
            actor_user_id=actor.id if actor else None,
            target=str(user.id),
            detail={"email": user.email, **(detail or {})},
            ip=ip,
        )
