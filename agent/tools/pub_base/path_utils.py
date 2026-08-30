"""Shared path resolution utilities for file tools."""

import os
from pathlib import Path

from config import ROOT_DIR


class PathOutOfBoundsError(ValueError):
    """Raised when a resolved path escapes ROOT_DIR.

    File tools must NOT be able to read/write outside the project root;
    otherwise an LLM-triggered model can exfiltrate secrets (e.g. .env),
    overwrite arbitrary files, or read system paths. Absolute paths that
    resolve outside ROOT_DIR are rejected rather than silently allowed.
    """


def resolve_path(file_path: str) -> Path:
    """Resolve file_path against ROOT_DIR if relative; expand ~.

    Returns a *contained* absolute path: the result is guaranteed to be
    (or be a descendant of) ``ROOT_DIR``. Raises :class:`PathOutOfBoundsError`
    (a :class:`ValueError`) if the input resolves outside ROOT_DIR.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()
    if resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR):
        raise PathOutOfBoundsError(
            f"Path resolves outside project root and is not allowed: {resolved} (root={ROOT_DIR})"
        )
    return resolved
