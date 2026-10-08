"""Login business logic: setup, login, refresh rotation, revocation, WS tickets.

Enforcement rules live here so the middleware stays a thin gate:

* the runtime switch (``auth_settings.enabled``) is the source of truth, and an
  enabled switch with **no account** never enforces anything — locking a
  deployment out of its own app is not a security win;
* refresh tokens rotate on every use, and the superseded ``jti`` is blacklisted,
  so a stolen refresh token is worth one use at most;
* failures are deliberately uniform (``invalid_credentials``) and a missing user
  still pays one scrypt verification, so neither the message nor the timing
  reveals whether a username exists.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass

from loguru import logger

from config.features import AUTH
from server.DAO import auth_store
from server.utils import jwt_utils
from server.utils.password import hash_password, password_policy_error, verify_password

__all__ = [
    "AuthError",
    "clear_state_cache",
    "enforcement_active",
    "sole_account",
    "TokenPair",
    "consume_ws_ticket",
    "mint_ws_ticket",
    "start_blacklist_cleanup",
    "current_user",
    "disable_auth",
    "enable_auth",
    "has_account",
    "is_enabled",
    "login",
    "logout",
    "refresh",
    "setup_account",
    "status",
    "update_account",
]

#: A password hash used only to keep failed logins as slow as successful ones.
_DUMMY_HASH = hash_password("sherry-dummy-password-for-timing")

_MAX_USERNAME_LENGTH = 64


class AuthError(Exception):
    """A refusal carrying a client-safe code, message and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TokenPair:
    """A freshly issued access/refresh pair plus what the client needs to schedule."""

    access_token: str
    refresh_token: str
    expires_in: int
    user: dict


# ── state ────────────────────────────────────────────────────────────────────


#: ``(expiry_monotonic, enabled, has_account)`` — hot-path cache for the gate,
#: which asks on EVERY request. Explicit invalidation on every write makes the
#: TTL only a safety net, not the correctness mechanism.
_state_cache: tuple[float, bool, bool] | None = None
_STATE_CACHE_TTL_SECONDS = 2.0


def clear_state_cache() -> None:
    """Drop the cached switch/account state (used by writes and by tests)."""
    global _state_cache
    _state_cache = None


async def _state() -> tuple[bool, bool]:
    global _state_cache
    now = time.monotonic()
    if _state_cache is not None and now < _state_cache[0]:
        return _state_cache[1], _state_cache[2]
    settings = await auth_store.get_auth_settings()
    enabled = bool(settings["enabled"])
    account = await has_account()
    _state_cache = (now + _STATE_CACHE_TTL_SECONDS, enabled, account)
    return enabled, account


async def is_enabled() -> bool:
    enabled, _ = await _state()
    return enabled


async def has_account() -> bool:
    return await auth_store.count_users() > 0


async def enforcement_active() -> bool:
    """Whether a session is required for non-exempt clients.

    An enabled switch with no account cannot gate — there would be nothing to
    log in with, and locking a deployment out of its own app is not a win.
    """
    enabled, account = await _state()
    return bool(enabled and account)


async def sole_account() -> dict | None:
    """The one configured account (single-user product), or ``None``."""
    user = await auth_store.get_first_user()
    return _public_user(user) if user else None


async def status(*, remote_is_loopback: bool) -> dict:
    """What the client asks before deciding to show a login page.

    The exemption is the SAME rule the middleware applies: a loopback caller
    only escapes the gate while ``require_auth_non_loopback`` is on. Reporting
    "no login needed" to a client the gate then refuses would strand it on a
    redirect-free page whose every request 401s.
    """
    enabled, account = await _state()
    exempt = remote_is_loopback and bool(AUTH["require_auth_non_loopback"])
    return {
        "auth_enabled": enabled,
        # An enabled switch without an account cannot gate — nothing to log in with.
        "auth_required": bool(enabled and account and not exempt),
        "has_account": account,
    }


def _public_user(user: dict) -> dict:
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "created_at": int(user["created_at"]),
    }


def _validate_username(username: str) -> str:
    name = (username or "").strip()
    if not name:
        raise AuthError("username_required", "username is required")
    if len(name) > _MAX_USERNAME_LENGTH:
        raise AuthError(
            "username_too_long", f"username must be at most {_MAX_USERNAME_LENGTH} characters"
        )
    if any(ch.isspace() for ch in name):
        raise AuthError("username_invalid", "username must not contain whitespace")
    return name


def _validate_password(password: str) -> str:
    problem = password_policy_error(password, min_length=AUTH["min_password_length"])
    if problem:
        raise AuthError("password_policy", problem)
    return password


# ── account lifecycle ────────────────────────────────────────────────────────


async def setup_account(username: str, password: str) -> dict:
    """First-time account setup; enables login protection on success.

    Only possible while no user exists — afterwards the account is changed
    through :func:`update_account`, which requires the current password.
    """
    if await has_account():
        raise AuthError("account_exists", "an account is already configured", 409)
    name = _validate_username(username)
    _validate_password(password)
    try:
        user = await auth_store.create_user(name, hash_password(password))
    except Exception as exc:  # sqlite3.IntegrityError on a duplicate username
        raise AuthError("username_taken", "that username is already taken", 409) from exc
    await auth_store.set_auth_enabled(True)
    clear_state_cache()
    logger.info("auth: account '{}' created; login protection enabled", name)
    return _public_user(user)


async def _verified_user(username: str, password: str) -> dict:
    """Look up + verify; uniform refusal and uniform-ish timing for both misses."""
    user = await auth_store.get_user_by_username(username or "")
    stored = user["password_hash"] if user else _DUMMY_HASH
    ok = verify_password(password or "", stored)
    if not user or not ok:
        raise AuthError("invalid_credentials", "invalid username or password", 401)
    return user


async def _full_user(user: dict) -> dict:
    """The stored row (with the hash) behind a possibly-public user dict.

    Callers pass what they have — a session's public user or the sole account —
    and password verification must always run against the stored hash.
    """
    row = await auth_store.get_user_by_id(int(user.get("id", 0)))
    if row is None:
        raise AuthError("not_authenticated", "the account no longer exists", 401)
    return row


async def update_account(
    user: dict,
    *,
    password: str,
    new_username: str | None = None,
    new_password: str | None = None,
) -> dict:
    """Change the username and/or the password; both require the current password."""
    user = await _full_user(user)
    if not verify_password(password or "", user["password_hash"]):
        raise AuthError("invalid_password", "the current password is incorrect", 401)
    if new_username is None and new_password is None:
        raise AuthError("no_change_requested", "provide new_username or new_password")
    if new_username is not None:
        name = _validate_username(new_username)
        if name != user["username"]:
            try:
                await auth_store.update_username(int(user["id"]), name)
            except Exception as exc:  # duplicate username
                raise AuthError("username_taken", "that username is already taken", 409) from exc
    if new_password is not None:
        _validate_password(new_password)
        await auth_store.update_password(int(user["id"]), hash_password(new_password))
    refreshed = await auth_store.get_user_by_id(int(user["id"]))
    assert refreshed is not None
    logger.info("auth: account '{}' updated", refreshed["username"])
    return _public_user(refreshed)


async def enable_auth(user: dict, password: str) -> None:
    """Turn login protection back on (the account already exists).

    The current password is required, exactly like disabling: flipping the
    switch either way is a deliberate act, not a lucky click.
    """
    user = await _full_user(user)
    if not verify_password(password or "", user["password_hash"]):
        raise AuthError("invalid_password", "the current password is incorrect", 401)
    await auth_store.set_auth_enabled(True)
    clear_state_cache()
    logger.info("auth: login protection enabled by '{}'", user["username"])


async def disable_auth(user: dict, password: str) -> None:
    """Turn login protection off (the account row stays for a later re-enable)."""
    user = await _full_user(user)
    if not verify_password(password or "", user["password_hash"]):
        raise AuthError("invalid_password", "the current password is incorrect", 401)
    await auth_store.set_auth_enabled(False)
    clear_state_cache()
    logger.info("auth: login protection disabled by '{}'", user["username"])


# ── sessions ─────────────────────────────────────────────────────────────────


async def login(username: str, password: str) -> TokenPair:
    """Verify credentials and issue a fresh pair."""
    user = await _verified_user(username, password)
    return _issue_pair(user)


async def refresh(refresh_token: str) -> TokenPair:
    """Rotate: verify a refresh token, revoke it, issue a new pair."""
    payload = jwt_utils.decode_token(refresh_token, kind=jwt_utils.REFRESH)
    if payload is None:
        raise AuthError("invalid_refresh_token", "the refresh token is invalid or expired", 401)
    jti = str(payload.get("jti") or "")
    if await auth_store.is_jwt_blacklisted(jti):
        # A revoked refresh token being presented again means a replay (or a
        # stolen copy racing the real client): refuse without issuing anything.
        logger.warning("auth: revoked refresh token presented again (jti={})", jti)
        raise AuthError("invalid_refresh_token", "the refresh token is invalid or expired", 401)
    user_id = jwt_utils.user_id_of(payload)
    user = await auth_store.get_user_by_id(user_id) if user_id is not None else None
    if user is None:
        raise AuthError("invalid_refresh_token", "the refresh token is invalid or expired", 401)
    await auth_store.add_jwt_to_blacklist(jti, int(payload["exp"]))
    return _issue_pair(user)


def _issue_pair(user: dict) -> TokenPair:
    user_id = int(user["id"])
    username = str(user["username"])
    return TokenPair(
        access_token=jwt_utils.create_access_token(user_id, username),
        refresh_token=jwt_utils.create_refresh_token(user_id),
        expires_in=int(AUTH["session_ttl_seconds"]),
        user=_public_user(user),
    )


async def logout(access_token: str, refresh_token: str | None = None) -> None:
    """Revoke the presented tokens (both kinds, when both were presented)."""
    for token, kind in ((access_token, jwt_utils.ACCESS), (refresh_token, jwt_utils.REFRESH)):
        if not token:
            continue
        payload = jwt_utils.decode_token(token, kind=kind)
        if payload is not None:
            await auth_store.add_jwt_to_blacklist(
                str(payload.get("jti") or ""), int(payload["exp"])
            )


async def current_user(access_token: str) -> dict | None:
    """The user behind a valid, unrevoked access token (``None`` = not signed in)."""
    payload = jwt_utils.decode_token(access_token, kind=jwt_utils.ACCESS)
    if payload is None:
        return None
    if await auth_store.is_jwt_blacklisted(str(payload.get("jti") or "")):
        return None
    user_id = jwt_utils.user_id_of(payload)
    if user_id is None:
        return None
    user = await auth_store.get_user_by_id(user_id)
    return _public_user(user) if user else None


# ── WebSocket tickets ────────────────────────────────────────────────────────


#: ``ticket -> expiry epoch seconds``. In-process only: a restart drops every
#: ticket, which is exactly the lifetime bound we want for a handshake token.
_tickets: dict[str, float] = {}


def _prune_tickets(now: float) -> None:
    expired = [key for key, expiry in _tickets.items() if expiry <= now]
    for key in expired:
        _tickets.pop(key, None)
    cap = int(AUTH["ws_ticket_cache_size"])
    while len(_tickets) > cap:
        # Dict order is insertion order: drop the oldest minted ticket first.
        _tickets.pop(next(iter(_tickets)), None)


async def mint_ws_ticket() -> tuple[str, int]:
    """Mint a single-use WebSocket handshake ticket.

    Authorization is the caller's business: the route decides whether this
    client may have one (a signed-in remote caller, or a trusted local one).
    """
    now = time.time()
    _prune_tickets(now)
    ticket = uuid.uuid4().hex
    ttl = int(AUTH["ws_ticket_ttl_seconds"])
    _tickets[ticket] = now + ttl
    return ticket, ttl


async def consume_ws_ticket(ticket: str) -> bool:
    """Validate and burn a ticket (single use, short TTL)."""
    if not ticket:
        return False
    now = time.time()
    _prune_tickets(now)
    expiry = _tickets.pop(ticket, None)
    return expiry is not None and expiry > now


# ── janitor ──────────────────────────────────────────────────────────────────


_cleanup_thread: threading.Thread | None = None


def start_blacklist_cleanup(interval_seconds: float = 300.0) -> None:
    """Start the once-per-process janitor that drops expired blacklist rows.

    A daemon thread with its own short-lived loops: revoked tokens only matter
    until their own ``exp``, so this is housekeeping — it must never block boot,
    hold the server's loop, or delay shutdown.
    """

    global _cleanup_thread
    if _cleanup_thread is not None and _cleanup_thread.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                asyncio.run(auth_store.cleanup_expired_blacklist())
            except Exception:  # noqa: BLE001 — housekeeping must not kill the thread
                logger.debug("auth: blacklist cleanup failed", exc_info=True)
            time.sleep(interval_seconds)

    _cleanup_thread = threading.Thread(target=_run, daemon=True, name="auth-blacklist-cleanup")
    _cleanup_thread.start()
