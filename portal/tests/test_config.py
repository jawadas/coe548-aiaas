"""Settings: the environment variable names, the defaults and what is refused."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from app import config
from app.config import ConfigError, Settings, get_settings, load_settings, origin_of

GOOD_SECRET = "Zq7mB2xW9tLr4HvN8cJd3KsF6gYp1TuA5eRi0oXw"
DATABASE_URL = "postgresql://portal:pw@postgres:5432/portal"
REQUIRED_ENV = {
    "PORTAL_PUBLIC_URL": "https://portal.test",
    "PORTAL_DATABASE_URL": DATABASE_URL,
    "PORTAL_SECRET_KEY": GOOD_SECRET,
    "LITELLM_MASTER_KEY": "sk-master",
}


def production(**overrides: Any) -> Settings:
    values = {
        "env": "production",
        "public_url": "https://portal.test",
        "database_url": DATABASE_URL,
        "secret_key": GOOD_SECRET,
        "litellm_master_key": "sk-master",
    }
    return Settings(**{**values, **overrides})


def development(**overrides: Any) -> Settings:
    values = {"env": "development", "database_url": DATABASE_URL, "litellm_master_key": "sk-master"}
    return Settings(**{**values, **overrides})


def set_env(monkeypatch: pytest.MonkeyPatch, **variables: str) -> None:
    for name, value in {**REQUIRED_ENV, **variables}.items():
        monkeypatch.setenv(name, value)


def test_defaults_are_the_documented_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    settings = Settings()
    assert settings.env == "production"
    assert settings.litellm_base_url == "http://litellm:4000"
    assert settings.model == "gpt-oss-20b"
    assert settings.invite_code is None
    assert settings.open_signup is False
    assert settings.admin_email_set == frozenset()
    assert settings.first_user_admin is True
    assert settings.cookie_secure is True
    assert settings.session_hours == 168
    assert settings.key_rpm_limit == 10
    assert settings.key_tpm_limit == 30000
    assert settings.user_rpm_limit == 30
    assert settings.user_tpm_limit == 60000
    assert settings.playground_rpm_limit == 10
    assert settings.max_keys_per_user == 5
    assert settings.auth_rate_limit is True


def test_every_setting_is_read_from_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(
        monkeypatch,
        PORTAL_ENV="development",
        PORTAL_PUBLIC_URL="http://localhost:8088",
        LITELLM_BASE_URL="http://127.0.0.1:4000/",
        PORTAL_MODEL="other-model",
        PORTAL_INVITE_CODE="  let-me-in ",
        PORTAL_OPEN_SIGNUP="true",
        PORTAL_ADMIN_EMAILS=" Boss@Example.com, ,second@example.com",
        PORTAL_FIRST_USER_ADMIN="false",
        PORTAL_COOKIE_SECURE="false",
        PORTAL_SESSION_HOURS="12",
        PORTAL_KEY_RPM_LIMIT="11",
        PORTAL_KEY_TPM_LIMIT="12",
        PORTAL_USER_RPM_LIMIT="13",
        PORTAL_USER_TPM_LIMIT="14",
        PORTAL_PLAYGROUND_RPM_LIMIT="15",
        PORTAL_MAX_KEYS_PER_USER="16",
        PORTAL_AUTH_RATE_LIMIT="false",
    )
    settings = Settings()
    assert settings.env == "development"
    assert settings.public_url == "http://localhost:8088"
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.secret_key.get_secret_value() == GOOD_SECRET
    assert settings.litellm_base_url == "http://127.0.0.1:4000"
    assert settings.litellm_master_key.get_secret_value() == "sk-master"
    assert settings.model == "other-model"
    assert settings.invite_code is not None
    assert settings.invite_code.get_secret_value() == "let-me-in"
    assert settings.open_signup is True
    assert settings.admin_email_set == {"boss@example.com", "second@example.com"}
    assert settings.first_user_admin is False
    assert settings.cookie_secure is False
    assert settings.session_lifetime == dt.timedelta(hours=12)
    assert (settings.key_rpm_limit, settings.key_tpm_limit) == (11, 12)
    assert (settings.user_rpm_limit, settings.user_tpm_limit) == (13, 14)
    assert settings.playground_rpm_limit == 15
    assert settings.max_keys_per_user == 16
    assert settings.auth_rate_limit is False


def test_blank_variables_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose passes ``${PORTAL_INVITE_CODE:-}``: an empty string, not an absent variable."""
    set_env(
        monkeypatch,
        PORTAL_INVITE_CODE="",
        PORTAL_ADMIN_EMAILS="",
        PORTAL_MODEL="",
        PORTAL_OPEN_SIGNUP="",
        PORTAL_SESSION_HOURS="",
    )
    settings = Settings()
    assert settings.invite_code is None
    assert settings.model == "gpt-oss-20b"
    assert settings.open_signup is False
    assert settings.session_hours == 168
    assert settings.signup_mode == "closed"


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "short",
        "x" * 31,
        "changeme",
        "changeme" * 5,
        "Change-Me-Change-Me-Change-Me-Change-Me",
        "please-use-a-real-secret-key-here-0123456789",
        config.DEV_SECRET_KEY,
        "a" * 40,
    ],
)
def test_production_refuses_a_missing_short_or_placeholder_secret_key(secret: str) -> None:
    with pytest.raises(ValidationError, match="PORTAL_SECRET_KEY"):
        production(secret_key=secret)


def test_production_accepts_a_random_secret_key() -> None:
    assert production().secret_key.get_secret_value() == GOOD_SECRET


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"public_url": ""}, "PORTAL_PUBLIC_URL must be set"),
        ({"public_url": "https://portal.test/app"}, "PORTAL_PUBLIC_URL must be an origin"),
        ({"public_url": "portal.test"}, "PORTAL_PUBLIC_URL must be an origin"),
        ({"database_url": ""}, "PORTAL_DATABASE_URL"),
        ({"database_url": "mysql://nope"}, "PORTAL_DATABASE_URL"),
        ({"litellm_master_key": ""}, "LITELLM_MASTER_KEY must be set"),
        ({"litellm_base_url": "litellm:4000"}, "LITELLM_BASE_URL"),
    ],
)
def test_production_refuses_an_incomplete_configuration(
    override: dict[str, str], problem: str
) -> None:
    with pytest.raises(ValidationError, match=problem):
        production(**override)


@pytest.mark.parametrize(
    "variable",
    ["PORTAL_KEY_RPM_LIMIT", "PORTAL_USER_TPM_LIMIT", "PORTAL_SESSION_HOURS"],
)
def test_limits_must_be_positive(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    set_env(monkeypatch, **{variable: "0"})
    with pytest.raises(ValidationError):
        Settings()


def test_development_fills_in_a_secret_key_and_a_public_url() -> None:
    settings = development()
    assert settings.secret_key.get_secret_value() == config.DEV_SECRET_KEY
    assert settings.public_url == config.DEV_PUBLIC_URL
    assert not settings.is_production


def test_public_url_loses_its_trailing_slash() -> None:
    settings = production(public_url="https://Portal.Test/")
    assert settings.public_url == "https://Portal.Test"
    assert settings.public_origin == "https://portal.test"


def test_sign_up_is_closed_unless_opened_or_given_an_invite_code() -> None:
    assert production().signup_mode == "closed"
    assert production(invite_code="   ").signup_mode == "closed"
    assert production(invite_code="team-5").signup_mode == "invite"
    assert production(open_signup=True).signup_mode == "open"
    assert production(open_signup=True, invite_code="team-5").signup_mode == "open"


def test_session_cookie_name_follows_cookie_secure() -> None:
    assert production().session_cookie_name == "__Host-aiaas_session"
    assert production(cookie_secure=False).session_cookie_name == "aiaas_session"


def test_secrets_stay_out_of_repr() -> None:
    settings = production(invite_code="team-5-invite")
    shown = repr(settings) + str(settings)
    for secret in (GOOD_SECRET, "sk-master", "team-5-invite", "portal:pw"):
        assert secret not in shown


def test_load_settings_explains_what_is_wrong_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(
        monkeypatch, PORTAL_SECRET_KEY="changeme-changeme-changeme-changeme", LITELLM_MASTER_KEY=""
    )
    monkeypatch.delenv("LITELLM_MASTER_KEY")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "PORTAL_SECRET_KEY looks like a placeholder" in message
    assert "LITELLM_MASTER_KEY must be set" in message
    assert "changeme-changeme" not in message


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    get_settings.cache_clear()
    try:
        first = get_settings()
        monkeypatch.setenv("PORTAL_MODEL", "changed-later")
        assert get_settings() is first
        assert first.model == "gpt-oss-20b"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Example.com", "https://example.com"),
        ("https://example.com:443/path?q=1", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("http://localhost:8088/login", "http://localhost:8088"),
        ("http://[::1]:8010/x", "http://[::1]:8010"),
        ("ftp://example.com", None),
        ("example.com", None),
        ("null", None),
        ("", None),
        ("http://example.com:notaport", None),
    ],
)
def test_origin_of(url: str, origin: str | None) -> None:
    assert origin_of(url) == origin
