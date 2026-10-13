"""scrypt password hashing (standard library only, no external dependency).

Storage format: ``scrypt$N$r$p$base64(salt)$base64(derived_key)`` — the cost
parameters travel with the hash, so raising them later does not invalidate
stored credentials. Verification re-derives with the STORED parameters and
compares in constant time (``hmac.compare_digest``).

A malformed or absurdly expensive stored value is rejected as a failed
verification rather than an exception: the hash column is server-side data, but
a crafted row must not turn a login attempt into a CPU bomb or a 500.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

__all__ = ["hash_password", "password_policy_error", "verify_password"]

_PREFIX = "scrypt"
#: OWASP-recommended floor for interactive logins.
DEFAULT_N = 16384
DEFAULT_R = 8
DEFAULT_P = 1
DEFAULT_DKLEN = 32
_SALT_BYTES = 16
#: Sanity ceilings for values read back from storage (see module docstring).
_MAX_N = 2**20
_MAX_R = 32
_MAX_P = 32
_MAX_DKLEN = 128


def password_policy_error(plain: str, *, min_length: int = 8) -> str | None:
    """Return a human-readable policy violation, or ``None`` when acceptable.

    The policy is length only — character-class rules buy little against an
    offline scrypt guess and cost real UX friction.
    """
    if not isinstance(plain, str) or len(plain) < min_length:
        return f"password must be at least {min_length} characters"
    if len(plain) > 1024:
        return "password is too long"
    return None


def hash_password(
    plain: str,
    *,
    n: int = DEFAULT_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
    dklen: int = DEFAULT_DKLEN,
) -> str:
    """Hash ``plain`` with a fresh random salt and return the storage string."""
    if not plain:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
    return "{}${}${}${}${}${}".format(
        _PREFIX,
        n,
        r,
        p,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time verification of ``plain`` against a stored hash string."""
    if not plain or not stored:
        return False
    try:
        prefix, raw_n, raw_r, raw_p, raw_salt, raw_hash = stored.split("$")
        if prefix != _PREFIX:
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = base64.b64decode(raw_salt, validate=True)
        expected = base64.b64decode(raw_hash, validate=True)
    except (ValueError, TypeError):
        return False
    if not (0 < n <= _MAX_N) or not (0 < r <= _MAX_R) or not (0 < p <= _MAX_P):
        return False
    if not salt or not (0 < len(expected) <= _MAX_DKLEN):
        return False
    try:
        derived = hashlib.scrypt(
            plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)
