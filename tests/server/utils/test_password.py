"""Password hashing: scrypt with a per-hash salt, constant-time verification.

Contract under test: the storage string carries its own parameters, salts are
unique per hash, the same password never produces the same stored value twice,
verification rejects tampered/malformed/absurd parameter sets instead of
raising, and the policy is length-only.
"""

from __future__ import annotations

import base64

import pytest

from server.utils.password import (
    DEFAULT_DKLEN,
    DEFAULT_N,
    DEFAULT_P,
    DEFAULT_R,
    hash_password,
    password_policy_error,
    verify_password,
)

pytestmark = [pytest.mark.unit]


def test_stored_format_carries_parameters_and_salt() -> None:
    stored = hash_password("correct horse battery staple")

    prefix, n, r, p, salt_b64, hash_b64 = stored.split("$")
    assert prefix == "scrypt"
    assert (int(n), int(r), int(p)) == (DEFAULT_N, DEFAULT_R, DEFAULT_P)
    assert len(base64.b64decode(salt_b64)) == 16
    assert len(base64.b64decode(hash_b64)) == DEFAULT_DKLEN
    assert "correct horse battery staple" not in stored


def test_verify_accepts_the_right_password_and_rejects_others() -> None:
    stored = hash_password("s3cret-passphrase")

    assert verify_password("s3cret-passphrase", stored) is True
    assert verify_password("s3cret-passphrasE", stored) is False
    assert verify_password("", stored) is False


def test_each_hash_uses_a_fresh_salt() -> None:
    first = hash_password("same-password")
    second = hash_password("same-password")

    assert first != second
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)


def test_custom_cost_parameters_round_trip() -> None:
    stored = hash_password("pw", n=1024, r=4, p=2, dklen=16)

    assert stored.split("$")[1:4] == ["1024", "4", "2"]
    assert verify_password("pw", stored) is True


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        "bcrypt$16384$8$1$AAAA$AAAA",
        "scrypt$notanint$8$1$AAAA$AAAA",
        "scrypt$16384$8$1$!!!not-base64!!!$AAAA",
        "scrypt$16384$8$1$" + base64.b64encode(b"salt").decode() + "$",
    ],
)
def test_malformed_stored_values_are_a_failed_verification(stored: str) -> None:
    assert verify_password("anything", stored) is False


def test_absurd_parameters_are_refused_instead_of_burning_cpu() -> None:
    """A crafted row must not turn a login attempt into a CPU bomb."""
    salt = base64.b64encode(b"0123456789abcdef").decode()
    digest = base64.b64encode(b"x" * 32).decode()

    assert verify_password("pw", f"scrypt${2**24}$8$1${salt}${digest}") is False
    assert verify_password("pw", f"scrypt$16384$9999$1${salt}${digest}") is False
    assert verify_password("pw", f"scrypt$16384$8$9999${salt}${digest}") is False


def test_every_stored_value_rejects_the_empty_password() -> None:
    stored = hash_password("real-password")

    assert verify_password("", stored) is False


def test_policy_is_length_only() -> None:
    assert password_policy_error("1234567", min_length=8) is not None
    assert password_policy_error("12345678", min_length=8) is None
    # No character-class rules: an all-letter long password is fine.
    assert password_policy_error("abcdefghijklmnop", min_length=8) is None
    assert password_policy_error("x" * 2000, min_length=8) is not None


def test_hash_password_refuses_an_empty_password() -> None:
    with pytest.raises(ValueError):
        hash_password("")
