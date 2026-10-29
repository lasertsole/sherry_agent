"""Anthropic prompt-cache control (explicit ``cache_control`` breakpoints).

Anthropic caches a request's prefix only when the CALLER marks it: a message
block, a tool, or the system prompt must carry ``cache_control: {"type":
"ephemeral"}``. Every other provider sherry supports caches automatically
(DeepSeek / OpenAI server side), which is why the rest of the architecture —
the frozen system prompt, the stable tool face, the reminder block pushed down
to the message layer — was built without any cache plumbing. On Anthropic that
architecture produced no cache hits at all; this registry is the switch the
wrapper (``models/LLMs/anthropic_cache.py``) reads.

The knobs are environment-driven because a packaged install's code tree is
read-only (``SHERRY_ANTHROPIC_CACHE`` / ``SHERRY_ANTHROPIC_CACHE_TTL`` /
``SHERRY_ANTHROPIC_CACHE_MAX_BREAKPOINTS``), and they are read at import like
every other feature registry: a change takes effect on the next backend start.
"""

import os
from typing import TypedDict

#: Anthropic's hard limit per request.
_ANTHROPIC_BREAKPOINT_LIMIT = 4


def _flag(name: str, default: bool) -> bool:
    """Env-driven switch; empty/unset keeps *default*."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _ttl(name: str, default: str) -> str:
    """``short`` (5 min, default) or ``long`` (1 h) — anything else keeps *default*."""
    raw = os.getenv(name, "").strip().lower()
    return raw if raw in {"short", "long"} else default


def _breakpoints(name: str) -> int:
    """Breakpoint budget, clamped to Anthropic's limit so a typo can never 400."""
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _ANTHROPIC_BREAKPOINT_LIMIT
    return max(0, min(value, _ANTHROPIC_BREAKPOINT_LIMIT))


class AnthropicCacheConfig(TypedDict):
    """Explicit prompt caching for the Anthropic provider."""

    #: Master switch; off makes the wrapper a byte-for-byte pass-through.
    enabled: bool
    #: ``short`` → ``{"type": "ephemeral"}`` (5 min, 1.25x write);
    #: ``long`` → adds ``"ttl": "1h"`` (2x write, survives a coffee break).
    ttl: str
    #: Breakpoints placed per request, capped at Anthropic's limit of 4.
    max_breakpoints: int


ANTHROPIC_CACHE: AnthropicCacheConfig = {
    # ON by default for the Anthropic provider only: the wrapper is mounted for
    # that provider alone, so a non-Anthropic install sees no behavior change at
    # all. The default is `short` because a first write on a cold cache costs
    # 1.25x — a long TTL is the operator's call once the hit rate is known.
    "enabled": _flag("SHERRY_ANTHROPIC_CACHE", True),
    "ttl": _ttl("SHERRY_ANTHROPIC_CACHE_TTL", "short"),
    "max_breakpoints": _breakpoints("SHERRY_ANTHROPIC_CACHE_MAX_BREAKPOINTS"),
}
