"""Login business logic: setup, login, refresh rotation, revocation, WS tickets.

Enforcement rules live here so the middleware stays a thin gate:

* the runtime switch (``auth_settings.enabled``) is the source of truth, and an
  enabled switch with **no account** never enforces anything — locking a
  deployment out of its own app is not a security win;
* refresh tokens rotate on every use, and the superseded ``jti`` is blacklisted,
  so a stolen refresh token is worth one use at most;
* failures are deliberately uniform (``invalid_credentials``) and a missing
  account still pays one scrypt verification, so the message and the timing say
  nothing about whether an account exists;
* there is exactly ONE account and login takes a PASSWORD ONLY (no username to
  enumerate, nothing to guess);
* repeated failures from one client are throttled with an ESCALATING lockout
  (``AUTH["login_max_attempts"]`` / ``login_lockout_base_seconds`` doubling to
  ``login_lockout_max_seconds``), and every lockout is logged — a brute-force
  campaign against a single password is otherwise cheap to run offline.
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
    "clear_login_throttle",
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


class AuthError(Exception):
    """A refusal carrying a client-safe code, message and HTTP status."""

    def __init__(
        self, code: str, message: str, status: int = 400, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        #: Seconds the caller must wait (throttled login); ``None`` = not a wait.
        self.retry_after = retry_after


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


# ── login throttle (brute force) ─────────────────────────────────────────────


class _LoginThrottle:
    """Escalating lockout per client key, in memory.

    Deliberately process-local: it costs nothing on the happy path, needs no
    schema, and a restart is not an attack vector here (the attacker would need
    the ability to restart the backend, which already implies the host). A
    failure streak older than ``login_attempt_window_seconds`` is forgotten, so
    a typo tomorrow never inherits today's wait.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: key → (failure streak, monotonic time of the last failure, blocked-until monotonic)
        self._state: dict[str, tuple[int, float, float]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds the key must still wait (0 = may try now)."""
        now = time.monotonic()
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return 0
            _, last_at, blocked_until = entry
            if now - last_at > AUTH["login_attempt_window_seconds"]:
                self._state.pop(key, None)
                return 0
            return max(0, int(round(blocked_until - now)))

    def record_failure(self, key: str) -> int:
        """Count one failure; returns the seconds this key is now blocked for (0 = free)."""
        now = time.monotonic()
        with self._lock:
            entry = self._state.get(key)
            streak = (
                0
                if entry is None or now - entry[1] > AUTH["login_attempt_window_seconds"]
                else entry[0]
            )
            streak += 1
            blocked_until = 0.0
            if streak > AUTH["login_max_attempts"]:
                exponent = streak - AUTH["login_max_attempts"] - 1
                delay = min(
                    AUTH["login_lockout_base_seconds"] * (2**exponent),
                    AUTH["login_lockout_max_seconds"],
                )
                blocked_until = now + delay
            self._state[key] = (streak, now, blocked_until)
            return max(0, int(round(blocked_until - now)))

    def clear(self, key: str) -> None:
        """Forget a key's streak (a successful login is proof of the password)."""
        with self._lock:
            self._state.pop(key, None)

    def reset(self) -> None:
        """Drop every streak (tests / a password change)."""
        with self._lock:
            self._state.clear()


_throttle = _LoginThrottle()

#: Key used when the caller's address is unknown (all such callers share one bucket).
_UNKNOWN_CLIENT = "unknown"


def _client_key(client: str | None) -> str:
    """The throttle bucket for one caller (the socket address, normalized)."""
    return (client or "").strip() or _UNKNOWN_CLIENT


def clear_login_throttle() -> None:
    """Forget every failure streak (called when the password changes)."""
    _throttle.reset()


async def is_enabled() -> bool:
    enabled, _ = await _state()
    return enabled


async def has_account() -> bool:
    return await auth_store.account_exists()


async def enforcement_active() -> bool:
    """Whether a session is required for non-exempt clients.

    An enabled switch with no account cannot gate — there would be nothing to
    log in with, and locking a deployment out of its own app is not a win.
    """
    enabled, account = await _state()
    return bool(enabled and account)


async def sole_account() -> dict | None:
    """The one configured account (single-user product), or ``None``."""
    user = await auth_store.get_account()
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
    """What a client may know about the account: an id and when it was made."""
    return {
        "id": int(user["id"]),
        "created_at": int(user["created_at"]),
    }


def _validate_password(password: str) -> str:
    problem = password_policy_error(password, min_length=AUTH["min_password_length"])
    if problem:
        raise AuthError("password_policy", problem)
    return password


# ── account lifecycle ────────────────────────────────────────────────────────


async def setup_account(password: str) -> dict:
    """First-time setup: set THE password and enable login protection.

    Only possible while no account exists — afterwards the account is changed
    through :func:`update_account`, which requires the current password.
    """
    if await has_account():
        raise AuthError("account_exists", "an account is already configured", 409)
    _validate_password(password)
    user = await auth_store.set_account_password(hash_password(password))
    await auth_store.set_auth_enabled(True)
    clear_state_cache()
    logger.info("auth: account created; login protection enabled")
    return _public_user(user)


async def _verified_user(password: str, client: str | None) -> dict:
    """Verify THE password; uniform refusal and uniform-ish timing for a miss.

    The throttle is checked BEFORE any hashing (so a locked-out caller cannot
    even make the process spin scrypt), and a missing account still pays one
    dummy verification — an unconfigured backend must not answer faster than a
    configured one.
    """
    key = _client_key(client)
    waiting = _throttle.retry_after(key)
    if waiting:
        logger.warning("auth: login throttled for {} ({}s left)", key, waiting)
        raise AuthError(
            "too_many_attempts",
            f"too many failed attempts — try again in {waiting}s",
            429,
            retry_after=waiting,
        )
    user = await auth_store.get_account()
    stored = user["password_hash"] if user else _DUMMY_HASH
    ok = verify_password(password or "", stored)
    if not user or not ok:
        blocked = _throttle.record_failure(key)
        if blocked:
            logger.warning("auth: login locked for {} after repeated failures ({}s)", key, blocked)
            raise AuthError(
                "too_many_attempts",
                f"too many failed attempts — try again in {blocked}s",
                429,
                retry_after=blocked,
            )
        logger.warning("auth: failed login from {}", key)
        raise AuthError("invalid_credentials", "incorrect password", 401)
    _throttle.clear(key)
    return user


async def _full_user(user: dict) -> dict:
    """The stored row (with the hash) behind a possibly-public user dict.

    Callers pass what they have — a session's public user or the sole account —
    and password verification must always run against the stored hash.
    """
    row = await auth_store.get_account()
    if row is None or int(row["id"]) != int(user.get("id", 0)):
        raise AuthError("not_authenticated", "the account no longer exists", 401)
    return row


async def update_account(user: dict, *, password: str, new_password: str | None = None) -> dict:
    """Change the password (the current one is required, and never throttled away)."""
    user = await _full_user(user)
    if not verify_password(password or "", user["password_hash"]):
        raise AuthError("invalid_password", "the current password is incorrect", 401)
    if new_password is None:
        raise AuthError("no_change_requested", "provide new_password")
    _validate_password(new_password)
    refreshed = await auth_store.set_account_password(hash_password(new_password))
    # The old streak was proof of a wrong password; the new one is not.
    clear_login_throttle()
    logger.info("auth: account password updated")
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
    logger.info("auth: login protection enabled")


async def disable_auth(user: dict, password: str) -> None:
    """Turn login protection off (the account row stays for a later re-enable)."""
    user = await _full_user(user)
    if not verify_password(password or "", user["password_hash"]):
        raise AuthError("invalid_password", "the current password is incorrect", 401)
    await auth_store.set_auth_enabled(False)
    clear_state_cache()
    logger.info("auth: login protection disabled")


# ── sessions ─────────────────────────────────────────────────────────────────


async def login(password: str, *, client: str | None = None) -> TokenPair:
    """Verify the password (throttled per client) and issue a fresh pair."""
    user = await _verified_user(password, client)
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
    account = await auth_store.get_account() if user_id is not None else None
    user = account if account is not None and int(account["id"]) == user_id else None
    if user is None:
        raise AuthError("invalid_refresh_token", "the refresh token is invalid or expired", 401)
    await auth_store.add_jwt_to_blacklist(jti, int(payload["exp"]))
    return _issue_pair(user)


def _issue_pair(user: dict) -> TokenPair:
    user_id = int(user["id"])
    return TokenPair(
        access_token=jwt_utils.create_access_token(user_id),
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
    account = await auth_store.get_account()
    if account is None or int(account["id"]) != int(user_id):
        return None
    return _public_user(account)


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
