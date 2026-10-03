"""JWT issuing/verification for the login feature (PyJWT, HS256 only).

Two token kinds, both carrying ``typ``: the middleware and the refresh endpoint
each pin the kind they accept, so a refresh token can never be replayed as an
access token (or the reverse). ``algorithms`` is pinned to the configured
algorithm — never ``none``, never an attacker-chosen alg.

The signing key is ``AUTH["jwt_secret"]`` when set, else a per-boot random
value: JWTs then die with the process, which is the safe default for a desktop
install and what ``SHERRY_AUTH_SECRET`` exists to change.
"""

from __future__ import annotations

import secrets
import time
import uuid

import jwt

from config.features import AUTH

__all__ = [
    "ACCESS",
    "REFRESH",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "jwt_secret",
]

#: Token kinds carried in the ``typ`` claim.
ACCESS = "access"
REFRESH = "refresh"

_BOOT_SECRET = secrets.token_urlsafe(48)


def jwt_secret() -> str:
    """The effective HMAC key (configured value, else the per-boot random one)."""
    return AUTH["jwt_secret"] or _BOOT_SECRET


def _encode(*, user_id: int, ttl_seconds: int, kind: str, username: str | None = None) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        # PyJWT validates that `sub` is a string (RFC 7519 §4.1.2), so the user
        # id travels as its decimal form and is converted back on read.
        "sub": str(user_id),
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
        "iss": AUTH["jwt_issuer"],
        "typ": kind,
    }
    if username is not None:
        payload["username"] = username
    return jwt.encode(payload, jwt_secret(), algorithm=AUTH["jwt_algorithm"])


def create_access_token(user_id: int, username: str) -> str:
    """Issue an access token for ``user_id``."""
    return _encode(
        user_id=user_id, ttl_seconds=AUTH["session_ttl_seconds"], kind=ACCESS, username=username
    )


def create_refresh_token(user_id: int) -> str:
    """Issue a refresh token for ``user_id`` (no username: it is not needed to rotate)."""
    return _encode(user_id=user_id, ttl_seconds=AUTH["refresh_ttl_seconds"], kind=REFRESH)


def decode_token(token: str, *, kind: str | None = None) -> dict | None:
    """Decode and fully validate a token; ``None`` means "reject".

    Rejections cover a bad signature, an expired or not-yet-valid token, a
    missing required claim, a wrong issuer and — when ``kind`` is given — a
    token of the other kind.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[AUTH["jwt_algorithm"]],
            issuer=AUTH["jwt_issuer"],
            options={"require": ["exp", "iat", "jti", "sub", "typ"]},
        )
    except jwt.PyJWTError:
        return None
    if kind is not None and payload.get("typ") != kind:
        return None
    return payload


def user_id_of(payload: dict) -> int | None:
    """The numeric user id from a decoded payload (``None`` when malformed)."""
    try:
        return int(str(payload.get("sub")))
    except (TypeError, ValueError):
        return None
