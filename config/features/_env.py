"""Shared private environment-reading helpers for the feature registry."""

from collections.abc import Mapping

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off", ""})


def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    """Read ``name`` from ``env`` as an int, falling back to ``default``.

    Accepts the boolean-like spellings the model-local flags use (``"true"`` /
    ``"false"``) so an import-time build never raises on a non-numeric value.
    """
    raw = env.get(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in _TRUE_ENV_VALUES:
        return 1
    if text in _FALSE_ENV_VALUES:
        return 0
    try:
        return int(text)
    except ValueError:
        return default
