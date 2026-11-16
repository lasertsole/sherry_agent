"""JWT, cookies and browser-origin checks for the Ingress."""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
from aiohttp import web

from .config import IngressConfig

ACCESS_TYPE = "access"
REFRESH_TYPE = "refresh"
_ALGORITHM = "HS256"


def access_cookie(config: IngressConfig) -> str:
    """Name of the access-token cookie."""
    return f"{config.cookie_name}_access"


def refresh_cookie(config: IngressConfig) -> str:
    """Name of the refresh-token cookie."""
    return f"{config.cookie_name}_refresh"


def _claims(user: dict[str, Any], *, kind: str, ttl: int, issuer: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": str(user["id"]),
        "username": user["username"],
        "tenant_id": user["tenant_id"],
        "role": user.get("role", "user"),
        "typ": kind,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "iss": issuer,
    }


def issue_tokens(
    user: dict[str, Any], config: IngressConfig
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Return ``(access, refresh, access_claims, refresh_claims)``."""
    access_claims = _claims(user, kind=ACCESS_TYPE, ttl=config.access_ttl_s, issuer=config.issuer)
    refresh_claims = _claims(
        user, kind=REFRESH_TYPE, ttl=config.refresh_ttl_s, issuer=config.issuer
    )
    access = jwt.encode(access_claims, config.jwt_secret, algorithm=_ALGORITHM)
    refresh = jwt.encode(refresh_claims, config.jwt_secret, algorithm=_ALGORITHM)
    return access, refresh, access_claims, refresh_claims


def decode_token(token: str, config: IngressConfig, *, expected: str) -> dict[str, Any] | None:
    """Verify a token and pin its type (access vs refresh)."""
    if not token:
        return None
    try:
        claims = jwt.decode(
            token,
            config.jwt_secret,
            algorithms=[_ALGORITHM],
            issuer=config.issuer,
            options={"require": ["sub", "exp", "typ", "jti"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != expected:
        return None
    return claims


def _cookie(request: web.Request, name: str) -> str:
    return str(request.cookies.get(name, "") or "")


def access_claims(request: web.Request, config: IngressConfig) -> dict[str, Any] | None:
    """Read the access token from the cookie or a Bearer header."""
    token = _cookie(request, access_cookie(config))
    if not token:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return decode_token(token, config, expected=ACCESS_TYPE)


def refresh_claims(request: web.Request, config: IngressConfig) -> dict[str, Any] | None:
    """Read the refresh token from its cookie or the header."""
    token = _cookie(request, refresh_cookie(config))
    if not token:
        token = str(request.headers.get("X-Refresh-Token", "") or "")
    return decode_token(token, config, expected=REFRESH_TYPE)


def set_auth_cookies(
    response: web.StreamResponse,
    *,
    access: str,
    refresh: str,
    config: IngressConfig,
) -> None:
    """Set the HttpOnly access/refresh cookies."""
    response.set_cookie(
        access_cookie(config),
        access,
        max_age=config.access_ttl_s,
        httponly=True,
        secure=config.secure_cookie,
        samesite="Lax",
        path="/",
    )
    response.set_cookie(
        refresh_cookie(config),
        refresh,
        max_age=config.refresh_ttl_s,
        httponly=True,
        secure=config.secure_cookie,
        samesite="Lax",
        path="/",
    )


def clear_auth_cookies(response: web.StreamResponse, config: IngressConfig) -> None:
    """Delete both auth cookies."""
    response.del_cookie(access_cookie(config), path="/")
    response.del_cookie(refresh_cookie(config), path="/")


def _same_origin(request: web.Request) -> str:
    forwarded = str(request.headers.get("X-Forwarded-Proto", "")).strip()
    scheme = forwarded or request.scheme
    return f"{scheme}://{request.host}"


def origin_allowed(request: web.Request, config: IngressConfig) -> bool:
    """Allow same-origin browsers and configured reverse-proxy origins."""
    origin = str(request.headers.get("Origin", "") or "").strip()
    if not origin:
        return True
    allowed = {item.rstrip("/").lower() for item in config.allowed_origins}
    if origin.rstrip("/").lower() in allowed:
        return True
    return origin.rstrip("/").lower() == _same_origin(request).rstrip("/").lower()


def csrf_ok(request: web.Request, config: IngressConfig) -> bool:
    """Check Origin/Sec-Fetch-Site for an unsafe request."""
    """Browser CSRF gate for unsafe methods; script clients (no Origin) pass."""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    if str(request.headers.get("Sec-Fetch-Site", "")).lower() == "cross-site":
        return False
    return origin_allowed(request, config)
