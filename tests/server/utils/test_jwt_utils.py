"""JWT issuing/verification: kind pinning, expiry, tampering, issuer checks.

Contract under test: an access token never validates as a refresh token (or the
reverse), a token signed with another key or edited by hand is rejected, an
expired token is rejected, required claims are enforced, and the signing key
follows ``AUTH['jwt_secret']`` when set (per-boot random otherwise).
"""

from __future__ import annotations

import time

import jwt
import pytest

from config.features import AUTH
from server.utils import jwt_utils

pytestmark = [pytest.mark.unit]


def test_access_token_carries_the_expected_claims() -> None:
    token = jwt_utils.create_access_token(7, "admin")
    payload = jwt_utils.decode_token(token, kind=jwt_utils.ACCESS)

    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["username"] == "admin"
    assert payload["typ"] == "access"
    assert payload["iss"] == AUTH["jwt_issuer"]
    assert payload["exp"] - payload["iat"] == AUTH["session_ttl_seconds"]
    assert jwt_utils.user_id_of(payload) == 7


def test_refresh_token_has_its_own_lifetime_and_kind() -> None:
    payload = jwt_utils.decode_token(jwt_utils.create_refresh_token(3), kind=jwt_utils.REFRESH)

    assert payload is not None
    assert payload["typ"] == "refresh"
    assert payload["exp"] - payload["iat"] == AUTH["refresh_ttl_seconds"]


def test_token_kinds_are_not_interchangeable() -> None:
    access = jwt_utils.create_access_token(1, "u")
    refresh = jwt_utils.create_refresh_token(1)

    assert jwt_utils.decode_token(access, kind=jwt_utils.REFRESH) is None
    assert jwt_utils.decode_token(refresh, kind=jwt_utils.ACCESS) is None
    # Without a kind pin both decode — callers must pin.
    assert jwt_utils.decode_token(access) is not None


def test_a_token_signed_with_another_key_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": "1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "x",
            "typ": "access",
        },
        "not-the-real-key",
        algorithm="HS256",
    )

    assert jwt_utils.decode_token(forged, kind=jwt_utils.ACCESS) is None


def test_a_hand_edited_token_is_rejected() -> None:
    token = jwt_utils.create_access_token(1, "u")
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}AA.{signature}"

    assert jwt_utils.decode_token(tampered, kind=jwt_utils.ACCESS) is None


def test_an_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(AUTH, "session_ttl_seconds", -1)
    expired = jwt_utils.create_access_token(1, "u")

    assert jwt_utils.decode_token(expired, kind=jwt_utils.ACCESS) is None


def test_a_missing_required_claim_is_rejected() -> None:
    # `issuer=` is a decode-only kwarg; the claim itself belongs in the payload.
    incomplete = jwt.encode(
        {"sub": "1", "typ": "access", "iss": AUTH["jwt_issuer"]},
        jwt_utils.jwt_secret(),
        algorithm=AUTH["jwt_algorithm"],
    )

    assert jwt_utils.decode_token(incomplete, kind=jwt_utils.ACCESS) is None


def test_a_foreign_issuer_is_rejected() -> None:
    foreign = jwt.encode(
        {
            "sub": "1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "x",
            "typ": "access",
            "iss": "somebody-else",
        },
        jwt_utils.jwt_secret(),
        algorithm=AUTH["jwt_algorithm"],
    )

    assert jwt_utils.decode_token(foreign, kind=jwt_utils.ACCESS) is None


def test_the_algorithm_is_pinned_so_none_is_never_accepted() -> None:
    unsigned = jwt.encode(
        {
            "sub": "1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "x",
            "typ": "access",
        },
        key="",
        algorithm="none",
    )

    assert jwt_utils.decode_token(unsigned, kind=jwt_utils.ACCESS) is None


def test_the_configured_secret_wins_over_the_boot_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(AUTH, "jwt_secret", "fixed-test-secret-0123456789abcdef")
    token = jwt_utils.create_access_token(1, "u")

    assert jwt_utils.jwt_secret() == "fixed-test-secret-0123456789abcdef"
    assert (
        jwt.decode(
            token,
            "fixed-test-secret-0123456789abcdef",
            algorithms=[AUTH["jwt_algorithm"]],
            issuer=AUTH["jwt_issuer"],
        )["sub"]
        == "1"
    )


def test_user_id_of_rejects_malformed_subjects() -> None:
    assert jwt_utils.user_id_of({"sub": "abc"}) is None
    assert jwt_utils.user_id_of({}) is None
    assert jwt_utils.user_id_of({"sub": "12"}) == 12
