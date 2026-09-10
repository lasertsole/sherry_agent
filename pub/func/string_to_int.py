"""Deterministic string -> integer ID helpers.

Merged from the former ``string_to_unique_int.py`` and
``rand_str_to_int.py`` modules (audit finding #30).

Both legacy entry points are kept **value-stable**: their outputs are
persisted (channel session ids in the message store, checkpointer
thread ids), so the underlying algorithms must never change.

- ``string_to_unique_int``  SHA-256, first 8 digest bytes as big-endian int.
- ``rand_str_to_int``       MD5, first ``slice_len`` hex chars parsed as hex.

New code should call :func:`string_to_int` with an explicit algorithm.
"""

import hashlib

_ALGORITHMS = ("sha256", "md5")


def string_to_int(s: str, algorithm: str = "sha256", slice_len: int = 8) -> int:
    """Convert a string to a deterministic non-negative integer.

    Args:
        s: Input string (utf-8 encoded).
        algorithm: ``"sha256"`` returns the first 8 digest bytes as a
            big-endian int (ignores ``slice_len``); ``"md5"`` returns the
            first ``slice_len`` hex chars of the digest parsed as hex.
        slice_len: Hex-char prefix length; only used by ``"md5"``.

    Raises:
        ValueError: Unknown ``algorithm``, or ``"md5"`` with an empty hex
            prefix (``slice_len=0``).
    """
    if algorithm not in _ALGORITHMS:
        raise ValueError(f"Unsupported algorithm {algorithm!r}; expected one of {_ALGORITHMS}")
    if algorithm == "sha256":
        return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:8], byteorder="big")
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:slice_len], 16)


def string_to_unique_int(s: str) -> int:
    """Legacy alias (SHA-256 based). Value-stable: do not change."""
    return string_to_int(s, algorithm="sha256")


def rand_str_to_int(s: str, slice_len: int = 8) -> int:
    """Legacy alias (MD5 based). Value-stable: do not change."""
    return string_to_int(s, algorithm="md5", slice_len=slice_len)
