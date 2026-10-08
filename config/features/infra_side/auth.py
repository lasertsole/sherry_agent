"""User-login configuration (``SHERRY_AUTH_*``).

Progressive by design: nothing enforces a login until an account is configured
through the menu — the runtime switch lives in SQLite (``auth_settings.enabled``),
not here — so a default install behaves exactly as before. When this config's
``enabled`` is true it only *seeds* a fresh database row: a deployment that wants
protection from the first boot sets ``SHERRY_AUTH_ENABLED=true``, creates the
account over loopback (always exempt), and remote access is then gated.

Secrets and hashing parameters:

* ``jwt_secret`` empty means a per-boot random key: every JWT dies with the
  process (fine for a desktop install, and the safe default).
* scrypt parameters are the OWASP-recommended floor (N=2^14, r=8, p=1).
"""

import os
from collections.abc import Mapping
from typing import TypedDict

__all__ = ["AuthConfig", "AUTH", "build_auth"]


class AuthConfig(TypedDict):
    """Login enforcement, JWT lifetimes, cookie shape and scrypt parameters."""

    #: Seed for a FRESH auth_settings row (env ``SHERRY_AUTH_ENABLED``). The
    #: runtime switch is the SQLite row the account menu writes.
    enabled: bool
    #: Loopback clients (the desktop app, a local browser) skip the login gate
    #: unless this is false. This is what keeps R1 painless.
    require_auth_non_loopback: bool
    #: Access-token lifetime in seconds (default 12h).
    session_ttl_seconds: int
    #: Refresh-token lifetime in seconds (default 7d).
    refresh_ttl_seconds: int
    jwt_algorithm: str
    #: HMAC key; empty = per-boot random (old JWTs stop verifying after restart).
    jwt_secret: str
    jwt_issuer: str
    #: Access token cookie (``Path=/``).
    cookie_name: str
    #: Refresh token cookie, scoped to the refresh endpoint so it is not sent
    #: with every request.
    refresh_cookie_name: str
    refresh_cookie_path: str
    cookie_path: str
    #: HTTPS-only cookies (``SHERRY_AUTH_COOKIE_SECURE``).
    cookie_secure: bool
    cookie_samesite: str
    #: WebSocket handshake tickets: single-use, short-lived, in-process only.
    ws_ticket_ttl_seconds: int
    ws_ticket_cache_size: int
    scrypt_n: int
    scrypt_r: int
    scrypt_p: int
    scrypt_dklen: int
    #: Shortest accepted password (the plan's policy: length only, no character
    #: classes, to keep the UX friction low).
    min_password_length: int


def _env_flag(source: Mapping[str, str], key: str, default: bool) -> bool:
    raw = source.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = source.get(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def build_auth(env: Mapping[str, str] | None = None) -> AuthConfig:
    """Build the auth config from the environment (injectable for tests)."""
    source = os.environ if env is None else env
    return AuthConfig(
        enabled=_env_flag(source, "SHERRY_AUTH_ENABLED", False),
        require_auth_non_loopback=_env_flag(source, "SHERRY_AUTH_REQUIRE_NON_LOOPBACK", True),
        session_ttl_seconds=_env_int(source, "SHERRY_AUTH_SESSION_TTL", 43200),
        refresh_ttl_seconds=_env_int(source, "SHERRY_AUTH_REFRESH_TTL", 604800),
        jwt_algorithm="HS256",
        jwt_secret=source.get("SHERRY_AUTH_SECRET", "").strip(),
        jwt_issuer="sherry-agent",
        cookie_name="sherry_session",
        refresh_cookie_name="sherry_refresh",
        refresh_cookie_path="/auth/refresh",
        cookie_path="/",
        cookie_secure=_env_flag(source, "SHERRY_AUTH_COOKIE_SECURE", False),
        cookie_samesite="strict",
        ws_ticket_ttl_seconds=_env_int(source, "SHERRY_AUTH_WS_TICKET_TTL", 30),
        ws_ticket_cache_size=256,
        scrypt_n=16384,
        scrypt_r=8,
        scrypt_p=1,
        scrypt_dklen=32,
        min_password_length=8,
    )


AUTH: AuthConfig = build_auth()
