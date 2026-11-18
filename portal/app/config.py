"""Portal configuration, read once from environment variables.

Every setting maps to one environment variable: ``PORTAL_<FIELD>`` for the
portal's own settings, plus ``LITELLM_BASE_URL`` and ``LITELLM_MASTER_KEY``.
Blank variables count as unset. Secrets are held as ``SecretStr`` so they never
show up in a ``repr`` or a log line by accident.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32
MIN_SECRET_KEY_DISTINCT_CHARS = 8
PLACEHOLDER_FRAGMENTS = (
    "changeme",
    "changethis",
    "placeholder",
    "example",
    "secretkey",
    "yoursecret",
    "password",
    "insecure",
    "default",
)

# Development conveniences. Neither is ever used when PORTAL_ENV=production.
DEV_SECRET_KEY = "dev-only-portal-key-never-deploy-0123456789abcdef"  # noqa: S105
DEV_PUBLIC_URL = "http://localhost:8010"

# Cookies other than the session cookie (whose name depends on PORTAL_COOKIE_SECURE).
CSRF_COOKIE = "aiaas_csrf"  # double-submit token for forms shown to anonymous visitors
FLASH_COOKIE = "aiaas_flash"  # one-time message carried across a redirect

SignupMode = Literal["open", "invite", "closed"]


class ConfigError(RuntimeError):
    """The environment does not describe a configuration the portal can run with."""


class Settings(BaseSettings):
    """All runtime settings. See the module docstring for the naming rule."""

    model_config = SettingsConfigDict(
        env_prefix="PORTAL_",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    env: Literal["production", "development"] = "production"
    public_url: str = ""
    database_url: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    litellm_base_url: str = Field(
        default="http://litellm:4000", validation_alias="LITELLM_BASE_URL"
    )
    litellm_master_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="LITELLM_MASTER_KEY"
    )
    model: str = "gpt-oss-20b"

    invite_code: SecretStr | None = None
    open_signup: bool = False
    admin_emails: str = ""
    first_user_admin: bool = True

    cookie_secure: bool = True
    session_hours: int = Field(default=168, ge=1, le=720)

    key_rpm_limit: int = Field(default=10, ge=1)
    key_tpm_limit: int = Field(default=30000, ge=1)
    user_rpm_limit: int = Field(default=30, ge=1)
    user_tpm_limit: int = Field(default=60000, ge=1)
    playground_rpm_limit: int = Field(default=10, ge=1)
    max_keys_per_user: int = Field(default=5, ge=0)

    auth_rate_limit: bool = True

    @field_validator("public_url", "litellm_base_url", "model", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("public_url", "litellm_base_url", mode="after")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("invite_code", mode="after")
    @classmethod
    def _blank_invite_is_unset(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return SecretStr(value.get_secret_value().strip())

    @model_validator(mode="after")
    def _validate(self) -> Self:
        problems = [*self._required_problems(), *self._secret_key_problems()]
        if self.public_url and origin_of(self.public_url) != self.public_url.lower():
            problems.append(
                "PORTAL_PUBLIC_URL must be an origin such as https://example.com "
                "(scheme and host, no path)"
            )
        if origin_of(self.litellm_base_url) is None:
            problems.append("LITELLM_BASE_URL must be an http(s) URL")
        if problems:
            raise ValueError("; ".join(problems))
        self._apply_development_defaults()
        return self

    def _required_problems(self) -> list[str]:
        problems = []
        database_url = self.database_url.get_secret_value()
        if not database_url.startswith(("postgresql://", "postgres://")):
            problems.append("PORTAL_DATABASE_URL must be set to a postgresql:// URL")
        if not self.litellm_master_key.get_secret_value().strip():
            problems.append("LITELLM_MASTER_KEY must be set")
        if not self.model:
            problems.append("PORTAL_MODEL must not be empty")
        if self.is_production and not self.public_url:
            problems.append("PORTAL_PUBLIC_URL must be set in production")
        return problems

    def _secret_key_problems(self) -> list[str]:
        if not self.is_production:
            return []
        reason = weak_secret_reason(self.secret_key.get_secret_value())
        return [f"PORTAL_SECRET_KEY {reason}"] if reason else []

    def _apply_development_defaults(self) -> None:
        if self.is_production:
            return
        if not self.secret_key.get_secret_value():
            self.secret_key = SecretStr(DEV_SECRET_KEY)
        if not self.public_url:
            self.public_url = DEV_PUBLIC_URL

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def public_origin(self) -> str:
        """Lower-cased ``scheme://host[:port]`` of the public URL, for the Origin check."""
        return origin_of(self.public_url) or ""

    @property
    def admin_email_set(self) -> frozenset[str]:
        """The normalised addresses listed in PORTAL_ADMIN_EMAILS."""
        parts = (part.strip().lower() for part in self.admin_emails.split(","))
        return frozenset(part for part in parts if part)

    @property
    def signup_mode(self) -> SignupMode:
        """Who may sign up. Without open sign-up or an invite code, nobody (fail closed)."""
        if self.open_signup:
            return "open"
        return "invite" if self.invite_code is not None else "closed"

    @property
    def session_lifetime(self) -> dt.timedelta:
        """How long a session lives without activity (PORTAL_SESSION_HOURS)."""
        return dt.timedelta(hours=self.session_hours)

    @property
    def session_cookie_name(self) -> str:
        """The ``__Host-`` prefix makes browsers refuse the cookie unless it is host-only."""
        return "__Host-aiaas_session" if self.cookie_secure else "aiaas_session"


def weak_secret_reason(value: str) -> str | None:
    """Say why ``value`` is not acceptable as a production secret, or ``None`` if it is."""
    if not value:
        return "must be set"
    if len(value) < MIN_SECRET_KEY_LENGTH:
        return f"must be at least {MIN_SECRET_KEY_LENGTH} characters long"
    squashed = "".join(char for char in value.lower() if char.isalnum())
    if value == DEV_SECRET_KEY or any(fragment in squashed for fragment in PLACEHOLDER_FRAGMENTS):
        return "looks like a placeholder; generate a random value"
    if len(set(value)) < MIN_SECRET_KEY_DISTINCT_CHARS:
        return "is too repetitive; generate a random value"
    return None


def origin_of(url: str) -> str | None:
    """Return ``scheme://host[:port]`` for an http(s) URL, or ``None`` if it is not one."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + ("" if port in (None, default_port) else f":{port}")


def load_settings() -> Settings:
    """Build settings from the environment, turning validation noise into one clear error."""
    try:
        return Settings()
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            lines.append(f"  - {where}: {message}" if where else f"  - {message}")
        raise ConfigError("Invalid portal configuration:\n" + "\n".join(lines)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, loaded on first use."""
    return load_settings()
