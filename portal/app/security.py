"""Cryptographic helpers: password hashing, random tokens, encryption and signed values.

Nothing in this module logs or returns secret material other than to its caller.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# argon2-cffi defaults: argon2id, 64 MiB, 3 passes, 4 lanes.
_HASHER: Final = PasswordHasher()
# A dedicated, small pool keeps hashing off the event loop and bounds how much
# memory a burst of logins can claim (64 MiB per hash in flight).
_HASH_POOL: Final = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")
# Verified when the email is unknown, so that path costs the same as a real check.
_DUMMY_HASH: Final = _HASHER.hash(secrets.token_urlsafe(16))


async def _in_hash_pool[T](function: Callable[..., T], *args: str) -> T:
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, function, *args)


async def hash_password(password: str) -> str:
    """Hash a password with argon2id in a worker thread."""
    return await _in_hash_pool(_HASHER.hash, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


async def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against its hash in a worker thread. Never raises on a mismatch."""
    return await _in_hash_pool(_verify, password_hash, password)


async def dummy_verify(password: str) -> None:
    """Spend the time of a real verification, for logins against unknown accounts."""
    await _in_hash_pool(_verify, _DUMMY_HASH, password)


def password_needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with parameters older than the current defaults."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def new_token(nbytes: int = 32) -> str:
    """A URL-safe random token carrying ``nbytes`` bytes of entropy."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    """Hex SHA-256 of a string. Used to store session tokens without keeping them."""
    return hashlib.sha256(value.encode()).hexdigest()


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Compare two strings without leaking where they differ. ``None`` never matches."""
    if left is None or right is None:
        return False
    return hmac.compare_digest(left.encode(), right.encode())


def safe_next_path(value: str | None, default: str = "/overview") -> str:
    """Return ``value`` if it is a same-site relative path, otherwise ``default``.

    Guards the post-login redirect against open redirects. Browsers read
    ``//host`` and ``/\\host`` as another site, and they drop tabs and newlines
    before parsing, so control characters are refused outright.
    """
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return default
    return value


def derive_key(secret_key: str, purpose: str) -> bytes:
    """Derive an independent 32-byte key from the application secret for one purpose."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode())
    return hkdf.derive(secret_key.encode())


class SecretBox:
    """Symmetric encryption (Fernet) for secrets the portal must be able to read back."""

    def __init__(self, secret_key: str) -> None:
        key = derive_key(secret_key, "aiaas-portal/secret-box/v1")
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str | None:
        """Return the plaintext, or ``None`` if the value is damaged or from another key."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, ValueError):
            return None


class Signer:
    """HMAC-signed, time-stamped values for data that travels through the browser.

    The value is readable by its holder but cannot be altered, and ``unsign``
    rejects it once it is older than ``max_age`` seconds.
    """

    def __init__(self, secret_key: str, purpose: str) -> None:
        self._key = derive_key(secret_key, f"aiaas-portal/signer/{purpose}/v1")

    def _signature(self, body: str) -> str:
        digest = hmac.new(self._key, body.encode(), hashlib.sha256).digest()
        return _b64encode(digest)

    def sign(self, value: str) -> str:
        body = f"{_b64encode(value.encode())}.{int(time.time())}"
        return f"{body}.{self._signature(body)}"

    def unsign(self, signed: str, max_age: int) -> str | None:
        """Return the original value, or ``None`` if it was tampered with or has expired."""
        body, _, signature = signed.rpartition(".")
        if not body or not constant_time_equals(signature, self._signature(body)):
            return None
        encoded, _, issued_at = body.partition(".")
        try:
            age = time.time() - int(issued_at)
            value = _b64decode(encoded).decode()
        except ValueError:
            return None
        return value if 0 <= age <= max_age else None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
