"""Per-model structured-output degradation labels (persisted).

A schema-bound child enforces its JSON shape at generation time. The DEFAULT is
LangChain's structured output (``create_agent(response_format=<schema>)``: a
structured-output tool with a forced ``tool_choice``). Some providers reject
that combination — DeepSeek in thinking mode answers ``400 Thinking mode does
not support this tool_choice`` — so the runner degrades ON ERROR, one step at a
time:

* level 0 — LangChain structured output (the default, untouched);
* level 1 — the child sends ``response_format={"type": "json_object"}`` itself,
  keeps the schema in its prompt, and the result is validated locally;
* level 2 — level 1 plus thinking OFF (some providers reject the forced
  ``tool_choice`` only while thinking).

The level is PERSISTED per ``(provider, model)`` with a re-probe clock: every
later subagent starts at the learned level instead of paying for the same
rejected request, but after 24 h → 48 h → 7 d the effective level decays one
rung so a provider that gained support can recover. A spawn that succeeds at the
decayed rung commits it (:func:`confirm_level`); re-reaching the same degraded
rung refreshes the clock with backoff instead of restarting it. Transient
429/rate-limit/timeout/connection errors never escalate. A missing/corrupt file
reads as "every model starts at 0", and a failed write is fail-open (the level
still applies for this process).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

__all__ = [
    "DEFAULT_LEVEL",
    "MAX_LEVEL",
    "StructuredOutputStrategy",
    "clear_all",
    "clear_cache",
    "clear_entry",
    "confirm_level",
    "escalate",
    "get_level",
    "level_label",
    "list_entries",
    "strategy_for",
]

DEFAULT_LEVEL = 0
MAX_LEVEL = 2

#: Re-probe backoff: after a degradation, the default rung is retried after
#: 24 h; a failed retry pushes the next attempt to 48 h, then one week. The
#: value is the age of ``escalated_at`` that lets ``get_level`` decay one rung.
_REPROBE_BACKOFF: tuple[float, ...] = (86_400.0, 172_800.0, 604_800.0)


@dataclass(frozen=True)
class StructuredOutputStrategy:
    """How a schema-bound child enforces its JSON shape at one degradation rung.

    The persisted label stays an integer (compatibility with
    ``subagent_model_caps.json``); this object is the single place the rung's
    BEHAVIOUR lives (2026-11-09 audit H2). Callers read flags, never compare
    ``level >= N``.
    """

    level: int
    label: str
    sends_json_mode: bool = False
    thinking_off: bool = False
    local_validation_fatal: bool = False


_STRATEGIES: dict[int, StructuredOutputStrategy] = {
    0: StructuredOutputStrategy(level=0, label="langchain-structured"),
    1: StructuredOutputStrategy(
        level=1,
        label="self-json-object",
        sends_json_mode=True,
        local_validation_fatal=True,
    ),
    2: StructuredOutputStrategy(
        level=2,
        label="self-json-object+thinking-off",
        sends_json_mode=True,
        thinking_off=True,
        local_validation_fatal=True,
    ),
}


def strategy_for(level: int) -> StructuredOutputStrategy:
    """The strategy for a persisted level, clamped to the supported range."""
    clamped = max(DEFAULT_LEVEL, min(MAX_LEVEL, int(level)))
    return _STRATEGIES[clamped]


_lock = threading.Lock()
#: Parsed ``{key: {"level": int, "reason": str, "updated_at": str}}``; ``None``
#: until the first read (a missing file caches as ``{}``).
_cache: dict[str, dict[str, Any]] | None = None


def _key(provider: str | None, model: str | None) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip()}"


def _path():
    # Imported lazily so a test that repoints SRC_DIR sees the new path.
    from config.path import subagent_model_caps_path

    return subagent_model_caps_path()


def _load_locked() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cache = {}
        return _cache
    if not isinstance(raw, dict):
        _cache = {}
        return _cache
    floor: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            level = int(entry.get("level", DEFAULT_LEVEL))
        except (TypeError, ValueError):
            continue
        floor[str(key)] = {
            "level": max(DEFAULT_LEVEL, min(MAX_LEVEL, level)),
            "reason": str(entry.get("reason") or "")[:300],
            "updated_at": str(entry.get("updated_at") or ""),
            "escalated_at": str(entry.get("escalated_at") or ""),
            "reprobe_count": _non_negative_int(entry.get("reprobe_count")),
        }
    _cache = floor
    return _cache


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    return 0


def _save_locked(entries: dict[str, dict[str, Any]]) -> None:
    from agent.tools.pub_base.atomic_write import atomic_write_text_no_follow

    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text_no_follow(path, json.dumps(entries, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - a label write must never fail a spawn
        logger.debug("subagent model caps: write failed (fail-open): {}", exc)


def _effective_level(entry: dict[str, Any], now: datetime) -> int:
    """The level a new spawn should use, applying the re-probe decay.

    Persistence is untouched here: a decayed rung is only committed by
    :func:`confirm_level` after a spawn actually succeeds at it. An entry
    written before ``escalated_at`` existed is treated as long overdue, so the
    next spawn re-probes the default rung once instead of staying degraded
    forever.
    """
    level = int(entry.get("level", DEFAULT_LEVEL))
    if level <= DEFAULT_LEVEL:
        return level
    escalated_at = str(entry.get("escalated_at") or "")
    if not escalated_at:
        return max(DEFAULT_LEVEL, level - 1)
    try:
        stamp = datetime.fromisoformat(escalated_at)
    except (TypeError, ValueError):
        return max(DEFAULT_LEVEL, level - 1)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    count = _non_negative_int(entry.get("reprobe_count"))
    interval = _REPROBE_BACKOFF[min(count, len(_REPROBE_BACKOFF) - 1)]
    try:
        age = (now - stamp).total_seconds()
    except (OverflowError, ValueError):
        return max(DEFAULT_LEVEL, level - 1)
    if age > interval:
        return max(DEFAULT_LEVEL, level - 1)
    return level


def get_level(provider: str | None, model: str | None) -> int:
    """The strategy level for the NEXT spawn of ``(provider, model)``.

    Decays one rung once the re-probe interval has passed (see
    :data:`_REPROBE_BACKOFF`); a spawn that then succeeds commits the lower
    level through :func:`confirm_level`.
    """
    with _lock:
        entry = _load_locked().get(_key(provider, model))
    if not entry:
        return DEFAULT_LEVEL
    return _effective_level(entry, datetime.now(UTC))


def escalate(provider: str | None, model: str | None, level: int, reason: str = "") -> int:
    """Raise the label to ``level`` and persist; returns the persisted level.

    Monotonic upward. Re-reaching the SAME rung after a decay refreshes
    ``escalated_at`` and advances ``reprobe_count``, so a provider that keeps
    rejecting the default rung is not re-probed on every spawn (24 h → 48 h →
    7 d).
    """
    target = max(DEFAULT_LEVEL, min(MAX_LEVEL, int(level)))
    key = _key(provider, model)
    now = datetime.now(UTC)
    with _lock:
        entries = _load_locked()
        entry = entries.get(key, {})
        current = int(entry.get("level", DEFAULT_LEVEL))
        if target > current:
            entries[key] = {
                "level": target,
                "reason": (reason or "")[:300],
                "updated_at": now.isoformat(timespec="seconds"),
                "escalated_at": now.isoformat(timespec="seconds"),
                "reprobe_count": 0,
            }
            _save_locked(entries)
            degraded = True
        elif target == current and current > DEFAULT_LEVEL:
            # The same degraded rung was rejected again after a decay: push the
            # next re-probe out instead of restarting the 24 h clock.
            entries[key] = {
                "level": current,
                "reason": (reason or entry.get("reason") or "")[:300],
                "updated_at": now.isoformat(timespec="seconds"),
                "escalated_at": now.isoformat(timespec="seconds"),
                "reprobe_count": min(
                    _non_negative_int(entry.get("reprobe_count")) + 1,
                    len(_REPROBE_BACKOFF) - 1,
                ),
            }
            _save_locked(entries)
            degraded = False
        else:
            return current
    if degraded:
        logger.warning(
            "subagent model caps: {} degraded to level {} ({})",
            key,
            target,
            (reason or "")[:160],
        )
    else:
        logger.debug(
            "subagent model caps: {} still rejects level {}; next re-probe backed off",
            key,
            max(DEFAULT_LEVEL, current - 1),
        )
    return target if degraded else current


def confirm_level(provider: str | None, model: str | None, level_used: int) -> None:
    """Commit a decayed level after a spawn succeeded at it.

    Called once per successful spawn. If the spawn ran BELOW the persisted
    level (the decay path), the lower level becomes the new truth. Landing on
    the default rung clears the entry's decay state; landing on an intermediate
    rung restarts its clock and advances the backoff. A spawn at the persisted
    level is a no-op — otherwise steady use would prevent the periodic re-probe.
    """
    used = max(DEFAULT_LEVEL, min(MAX_LEVEL, int(level_used)))
    key = _key(provider, model)
    now = datetime.now(UTC)
    with _lock:
        entries = _load_locked()
        entry = entries.get(key)
        if entry is None:
            return
        persisted = int(entry.get("level", DEFAULT_LEVEL))
        if used >= persisted:
            return
        count = _non_negative_int(entry.get("reprobe_count"))
        entries[key] = {
            "level": used,
            "reason": str(entry.get("reason") or ""),
            "updated_at": now.isoformat(timespec="seconds"),
            "escalated_at": ("" if used <= DEFAULT_LEVEL else now.isoformat(timespec="seconds")),
            "reprobe_count": (
                0 if used <= DEFAULT_LEVEL else min(count + 1, len(_REPROBE_BACKOFF) - 1)
            ),
        }
        _save_locked(entries)


def list_entries() -> list[dict[str, Any]]:
    """Every persisted degradation, with the level the next spawn would use.

    A level-0 row is kept on disk after a confirmed re-probe (it preserves the
    last reason and backoff counter) but is not a degradation any more, so the
    panel does not list it.
    """
    now = datetime.now(UTC)
    with _lock:
        entries = dict(_load_locked())
    result: list[dict[str, Any]] = []
    for key in sorted(entries):
        entry = entries[key]
        level = int(entry.get("level", DEFAULT_LEVEL))
        if level <= DEFAULT_LEVEL:
            continue
        provider, _, model = key.partition("/")
        result.append(
            {
                "provider": provider,
                "model": model,
                "level": level,
                "effective_level": _effective_level(entry, now),
                "reason": str(entry.get("reason") or ""),
                "updated_at": str(entry.get("updated_at") or ""),
                "escalated_at": str(entry.get("escalated_at") or ""),
                "reprobe_count": _non_negative_int(entry.get("reprobe_count")),
            }
        )
    return result


def clear_entry(provider: str | None, model: str | None) -> bool:
    """Forget one model's degradation (the next spawn starts at level 0)."""
    key = _key(provider, model)
    with _lock:
        entries = _load_locked()
        if key not in entries:
            return False
        del entries[key]
        _save_locked(entries)
    logger.info("subagent model caps: cleared {}", key)
    return True


def clear_all() -> int:
    """Forget every degradation; returns how many entries were removed."""
    with _lock:
        entries = _load_locked()
        count = len(entries)
        if count:
            entries.clear()
            _save_locked(entries)
    if count:
        logger.info("subagent model caps: cleared all {} entries", count)
    return count


def level_label(level: int) -> str:
    """A short human label for logs/records."""
    strategy = _STRATEGIES.get(int(level))
    return strategy.label if strategy is not None else f"level-{level}"


def clear_cache() -> None:
    """Drop the in-process cache (tests / an externally edited file)."""
    global _cache
    with _lock:
        _cache = None
