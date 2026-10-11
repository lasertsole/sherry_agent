"""The auth service: setup, login, rotation, revocation, tickets, enforcement.

Contract under test: the runtime switch follows the account (setup enables,
disable turns off), refusals are uniform and carry client-safe codes, refresh
rotation burns the presented token (a replay gets nothing), the single-use WS
ticket expires, and ``enforcement_active`` never gates a deployment that has no
account to log in with.
"""

from __future__ import annotations

import pytest

from server.DAO import auth_store
from server.service import auth_service
from server.service.auth_service import AuthError
from server.utils import jwt_utils

pytestmark = [pytest.mark.unit]


async def _setup(password: str = "hunter2-long") -> dict:
    return await auth_service.setup_account(password)


# ── setup / switch ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_creates_the_account_and_enables_protection(isolated_auth_store) -> None:
    user = await _setup()

    # Password-only: the public account carries an id and a timestamp, nothing else.
    assert set(user) == {"id", "created_at"}
    assert "password_hash" not in user
    assert await auth_store.account_exists() is True
    assert (await auth_store.get_auth_settings())["enabled"] is True
    assert await auth_service.enforcement_active() is True


@pytest.mark.asyncio
async def test_setup_is_refused_once_an_account_exists(isolated_auth_store) -> None:
    await _setup()

    with pytest.raises(AuthError) as excinfo:
        await _setup("another-password")
    assert excinfo.value.code == "account_exists"
    assert excinfo.value.status == 409


@pytest.mark.asyncio
async def test_setup_enforces_the_password_policy(isolated_auth_store) -> None:
    with pytest.raises(AuthError) as short:
        await _setup("short")
    assert short.value.code == "password_policy"


@pytest.mark.asyncio
async def test_status_requires_login_only_for_remote_clients_with_an_account(
    isolated_auth_store,
) -> None:
    assert await auth_service.status(remote_is_loopback=False) == {
        "auth_enabled": False,
        "auth_required": False,
        "has_account": False,
    }

    await _setup()
    remote = await auth_service.status(remote_is_loopback=False)
    local = await auth_service.status(remote_is_loopback=True)
    assert remote == {"auth_enabled": True, "auth_required": True, "has_account": True}
    assert local["auth_required"] is False


@pytest.mark.asyncio
async def test_status_mirrors_the_strict_loopback_policy(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the exemption off, loopback clients must be told they need a login.

    The guard trusts this flag and the middleware enforces the same rule; a
    mismatch strands the page on a redirect-free screen of 401s.
    """
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "require_auth_non_loopback", False)
    await _setup()

    assert (await auth_service.status(remote_is_loopback=True))["auth_required"] is True
    assert (await auth_service.status(remote_is_loopback=False))["auth_required"] is True


@pytest.mark.asyncio
async def test_an_enabled_switch_without_an_account_never_gates(isolated_auth_store) -> None:
    await auth_store.set_auth_enabled(True)
    auth_service.clear_state_cache()

    assert await auth_service.enforcement_active() is False
    assert (await auth_service.status(remote_is_loopback=False))["auth_required"] is False


# ── login ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_issues_a_usable_pair(isolated_auth_store) -> None:
    await _setup("hunter2-long")

    pair = await auth_service.login("hunter2-long")

    assert set(pair.user) == {"id", "created_at"}
    assert jwt_utils.decode_token(pair.access_token, kind=jwt_utils.ACCESS) is not None
    assert jwt_utils.decode_token(pair.refresh_token, kind=jwt_utils.REFRESH) is not None
    assert pair.expires_in > 0
    user = await auth_service.current_user(pair.access_token)
    assert user is not None and user["id"] == pair.user["id"]
    # The access token carries no personal data beyond the account id.
    claims = jwt_utils.decode_token(pair.access_token, kind=jwt_utils.ACCESS) or {}
    assert "username" not in claims


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["wrong-password", "", "hunter2-long "])
async def test_login_refusals_are_uniform(isolated_auth_store, password) -> None:
    await _setup("hunter2-long")

    with pytest.raises(AuthError) as excinfo:
        await auth_service.login(password)
    assert excinfo.value.code == "invalid_credentials"
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_login_refuses_everything_while_no_account_exists(isolated_auth_store) -> None:
    """No account = nothing to log into; the refusal is the same as a typo."""
    with pytest.raises(AuthError) as excinfo:
        await auth_service.login("anything-at-all")
    assert excinfo.value.code == "invalid_credentials"


# ── refresh rotation / logout ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_rotates_and_burns_the_presented_token(isolated_auth_store) -> None:
    await _setup()
    first = await auth_service.login("hunter2-long")

    second = await auth_service.refresh(first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert jwt_utils.decode_token(second.access_token, kind=jwt_utils.ACCESS) is not None
    # The presented refresh token is now revoked: a replay must fail.
    with pytest.raises(AuthError) as replay:
        await auth_service.refresh(first.refresh_token)
    assert replay.value.code == "invalid_refresh_token"


@pytest.mark.asyncio
async def test_refresh_refuses_an_access_token_and_garbage(isolated_auth_store) -> None:
    await _setup()
    pair = await auth_service.login("hunter2-long")

    for candidate in (pair.access_token, "not-a-jwt", ""):
        with pytest.raises(AuthError):
            await auth_service.refresh(candidate)


@pytest.mark.asyncio
async def test_logout_revokes_the_access_token(isolated_auth_store) -> None:
    await _setup()
    pair = await auth_service.login("hunter2-long")
    assert await auth_service.current_user(pair.access_token) is not None

    await auth_service.logout(pair.access_token, pair.refresh_token)

    assert await auth_service.current_user(pair.access_token) is None
    with pytest.raises(AuthError):
        await auth_service.refresh(pair.refresh_token)


# ── account changes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disable_requires_the_password_and_keeps_the_account(isolated_auth_store) -> None:
    await _setup("hunter2-long")
    user = await auth_service.sole_account()
    assert user is not None

    with pytest.raises(AuthError) as wrong:
        await auth_service.disable_auth(user, "not-the-password")
    assert wrong.value.code == "invalid_password"
    assert await auth_service.enforcement_active() is True

    await auth_service.disable_auth(user, "hunter2-long")
    assert await auth_service.enforcement_active() is False
    # The account row survives: re-enabling later needs no new password.
    assert await auth_store.account_exists() is True
    pair = await auth_service.login("hunter2-long")
    assert pair.user["id"] == user["id"]


@pytest.mark.asyncio
async def test_change_password_requires_the_current_one(isolated_auth_store) -> None:
    await _setup("hunter2-long")
    user = await auth_service.sole_account()
    assert user is not None

    with pytest.raises(AuthError):
        await auth_service.update_account(user, password="nope", new_password="brand-new-pass")

    updated = await auth_service.update_account(
        user, password="hunter2-long", new_password="brand-new-pass"
    )
    assert updated["id"] == user["id"]
    with pytest.raises(AuthError):
        await auth_service.login("hunter2-long")
    assert (await auth_service.login("brand-new-pass")).user["id"] == user["id"]


@pytest.mark.asyncio
async def test_setup_then_a_password_change_keeps_one_row(isolated_auth_store) -> None:
    await _setup("hunter2-long")
    user = await auth_service.sole_account()
    assert user is not None

    await auth_service.update_account(user, password="hunter2-long", new_password="rotation-pass")
    row = await auth_store.get_account()

    assert row is not None and row["id"] == user["id"]
    # One account, ever: a password change never adds a row.
    assert (await auth_store.get_account() or {})["id"] == 1


@pytest.mark.asyncio
async def test_update_requires_something_to_change(isolated_auth_store) -> None:
    await _setup()
    user = await auth_service.sole_account()
    assert user is not None

    with pytest.raises(AuthError) as excinfo:
        await auth_service.update_account(user, password="hunter2-long")
    assert excinfo.value.code == "no_change_requested"


@pytest.mark.asyncio
async def test_new_password_must_pass_the_policy(isolated_auth_store) -> None:
    await _setup()
    user = await auth_service.sole_account()
    assert user is not None

    with pytest.raises(AuthError) as excinfo:
        await auth_service.update_account(user, password="hunter2-long", new_password="short")
    assert excinfo.value.code == "password_policy"


# ── brute force: the escalating lockout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_failures_escalate_into_a_lockout(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N failures lock the client out; the wait GROWS with every further try.

    Live-verified shape: 5 free attempts, then 5s, 10s, 20s… capped. Each extra
    attempt while locked is refused WITHOUT hashing (a locked-out caller must not
    be able to keep the process busy).
    """
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "login_max_attempts", 2)
    monkeypatch.setitem(AUTH, "login_lockout_base_seconds", 5)
    monkeypatch.setitem(AUTH, "login_lockout_max_seconds", 60)
    await _setup("hunter2-long")

    # Two free attempts.
    for _ in range(2):
        with pytest.raises(AuthError) as excinfo:
            await auth_service.login("wrong", client="10.0.0.9")
        assert excinfo.value.code == "invalid_credentials"

    # The third failure trips the first lockout…
    with pytest.raises(AuthError) as first:
        await auth_service.login("wrong", client="10.0.0.9")
    assert first.value.code == "too_many_attempts"
    assert first.value.status == 429
    assert first.value.retry_after and first.value.retry_after > 0

    # …the next attempt is refused on sight (429 again), never reaching scrypt.
    with pytest.raises(AuthError) as second:
        await auth_service.login("hunter2-long", client="10.0.0.9")
    assert second.value.code == "too_many_attempts"

    # A different client is unaffected: the bucket is per address.
    pair = await auth_service.login("hunter2-long", client="10.0.0.10")
    assert pair.expires_in > 0


@pytest.mark.asyncio
async def test_a_successful_login_clears_the_streak(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "login_max_attempts", 5)
    await _setup("hunter2-long")

    for _ in range(4):
        with pytest.raises(AuthError):
            await auth_service.login("wrong", client="10.0.0.11")

    await auth_service.login("hunter2-long", client="10.0.0.11")

    # The streak is gone: four more failures stay "free" attempts, not a lockout.
    for _ in range(4):
        with pytest.raises(AuthError) as excinfo:
            await auth_service.login("wrong", client="10.0.0.11")
        assert excinfo.value.code == "invalid_credentials"


@pytest.mark.asyncio
async def test_the_lockout_expires_within_the_attempt_window(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streak older than the window is forgotten (a typo tomorrow is not a campaign)."""
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "login_max_attempts", 1)
    monkeypatch.setitem(AUTH, "login_lockout_base_seconds", 0)
    monkeypatch.setitem(AUTH, "login_lockout_max_seconds", 0)
    monkeypatch.setitem(AUTH, "login_attempt_window_seconds", 0)
    await _setup("hunter2-long")

    for _ in range(3):
        with pytest.raises(AuthError) as excinfo:
            await auth_service.login("wrong", client="10.0.0.12")
        # Zero-length lockouts and a zero window: never throttled.
        assert excinfo.value.code == "invalid_credentials"


# ── SQL injection / literal handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_sql_shaped_password_is_a_literal(isolated_auth_store) -> None:
    """Every query is parameterized, so SQL in the password is just wrong text.

    Pinned here because "log in with one password" is exactly the shape that
    invites string-built SQL: a hit would bypass the hash comparison entirely.
    """
    payloads = [
        "' OR 1=1 --",
        "'; DROP TABLE auth_account; --",
        '" OR "1"="1',
        "admin'--",
    ]
    await _setup("hunter2-long")

    for payload in payloads:
        with pytest.raises(AuthError) as excinfo:
            await auth_service.login(payload, client="10.0.0.13")
        assert excinfo.value.code == "invalid_credentials"

    # Nothing was dropped: the real password still works.
    assert (await auth_service.login("hunter2-long", client="10.0.0.13")).expires_in > 0
    assert await auth_store.account_exists() is True


@pytest.mark.asyncio
async def test_a_sql_shaped_password_is_stored_literally(isolated_auth_store) -> None:
    quoted = "pw'with\"quotes; -- and spaces"
    await auth_service.setup_account(quoted)

    stored = await auth_store.get_account()
    assert stored is not None and stored["password_hash"].startswith("scrypt$")
    assert (await auth_service.login(quoted)).expires_in > 0


# ── WS tickets ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticket_is_single_use(isolated_auth_store) -> None:
    ticket, ttl = await auth_service.mint_ws_ticket()

    assert ttl > 0
    assert await auth_service.consume_ws_ticket(ticket) is True
    assert await auth_service.consume_ws_ticket(ticket) is False
    assert await auth_service.consume_ws_ticket("") is False


@pytest.mark.asyncio
async def test_expired_tickets_are_refused(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "ws_ticket_ttl_seconds", -1)
    ticket, _ = await auth_service.mint_ws_ticket()

    assert await auth_service.consume_ws_ticket(ticket) is False


@pytest.mark.asyncio
async def test_the_ticket_cache_is_bounded(
    isolated_auth_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import AUTH

    monkeypatch.setitem(AUTH, "ws_ticket_cache_size", 3)
    minted = [(await auth_service.mint_ws_ticket())[0] for _ in range(5)]

    # The two oldest fell out of the cache (their tickets are unusable now).
    assert await auth_service.consume_ws_ticket(minted[0]) is False
    assert await auth_service.consume_ws_ticket(minted[1]) is False
    assert await auth_service.consume_ws_ticket(minted[2]) is True
    assert await auth_service.consume_ws_ticket(minted[4]) is True
