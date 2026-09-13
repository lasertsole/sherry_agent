"""Session key normalization between the announce side and the registry side.

The sub-agent EventBus carries prefixed main-session keys
(``agent:main:session:{id}`` — see ``events/bridge.py``) while the service
side (``server/service/messages.py``) and ``runtime/relation_register`` work
with bare session ids. These helpers convert between the two forms.

Tolerance contract:
    - ``None`` / empty / whitespace-only input normalizes to ``""``
    - surrounding whitespace is trimmed before prefix matching
    - the prefix is matched case-insensitively; the id portion is preserved verbatim
    - keys without the ``agent:main:session:`` prefix (bare ids, child keys
      like ``agent:{id}:subagent:{uuid}`` and ``agent:{id}:swarm:{g}:{uuid}``)
      are returned unchanged — only the main-session prefix is ever stripped
"""

from __future__ import annotations

SESSION_KEY_PREFIX = "agent:main:session:"


def normalize_session_key(raw: str) -> str:
    """Strip the ``agent:main:session:`` prefix and return the bare session id.

    Keys without the prefix are returned as-is; ``None``/empty/whitespace-only
    input yields ``""``. Character-compatible with
    ``events/bridge.py::_strip_session_prefix`` for non-empty string inputs.
    """
    if not raw:
        return ""
    trimmed = raw.strip()
    if trimmed.lower().startswith(SESSION_KEY_PREFIX):
        return trimmed[len(SESSION_KEY_PREFIX) :]
    return trimmed


def denormalize_session_key(bare: str) -> str:
    """Reassemble the prefixed ``agent:main:session:{id}`` form from a bare id.

    Already-prefixed keys are returned unchanged (no double prefix);
    ``None``/empty/whitespace-only input yields ``""``.
    """
    if not bare:
        return ""
    trimmed = bare.strip()
    if not trimmed:
        return ""
    if trimmed.lower().startswith(SESSION_KEY_PREFIX):
        return trimmed
    return SESSION_KEY_PREFIX + trimmed
