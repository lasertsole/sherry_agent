"""Session run report: token / context hit-rate / code-intel usage.

Pure post-task analysis over the session transcript — no hot-path hooks and no
background turn-end work (which would escape a turn's lifecycle and race test
fixtures). The report is generated on first API read and cached in-process; a
cache miss or stale entry simply regenerates from the live transcript.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from pub.func.session_report.code_intel_tracker import analyze_code_intel
from pub.func.session_report.context_analyzer import analyze_transcript
from pub.func.session_report.token_tracker import TokenLedger, session_tokens

#: Process-local cache; post-task analysis is cheap and the API regenerates on
#: miss. A restart simply means the next read rebuilds it — no persistence, no
#: state-register writes, no background task escaping the turn lifecycle.
_REPORT_CACHE: dict[str, dict[str, Any]] = {}


def _task_text(messages: list[Any]) -> str:
    """First human message is the task text the context analyzer keywords on."""
    for message in messages:
        role = getattr(message, "type", "") or ""
        if role == "human":
            content = getattr(message, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def _model_name(messages: list[Any]) -> str:
    """Model from the last AIMessage's response metadata, when present."""
    for message in reversed(messages):
        meta = getattr(message, "response_metadata", None) or {}
        model = meta.get("model_name") or meta.get("model")
        if model:
            return str(model)
    return ""


async def build_session_report(session_id: str) -> dict[str, Any]:
    """Build the report live from the session transcript (fail-open on read)."""
    from agent.state_port import read_messages

    try:
        messages = await read_messages(session_id)
    except Exception:  # noqa: BLE001 - no transcript -> an empty, valid report
        logger.debug("session report: transcript read failed for {}", session_id, exc_info=True)
        messages = []

    tokens = session_tokens(messages)
    ledger = TokenLedger(session_id)
    ledger.record(session_id, tokens, role="main_agent")

    return {
        "session_id": session_id,
        "generated_at": int(time.time()),
        "tokens": ledger.payload(),
        "context_hit_rate": analyze_transcript(messages, _task_text(messages)),
        "code_intel": analyze_code_intel(messages),
        "model": _model_name(messages),
    }


def get_cached_report(session_id: str) -> dict[str, Any] | None:
    return _REPORT_CACHE.get(session_id)


def cache_report(session_id: str, report: dict[str, Any]) -> None:
    _REPORT_CACHE[session_id] = report


async def get_session_report(session_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """API entry: cached report, regenerated live on miss or ``refresh``."""
    if not refresh:
        cached = get_cached_report(session_id)
        if cached is not None:
            return cached
    report = await build_session_report(session_id)
    cache_report(session_id, report)
    return report


def clear_session_report(session_id: str) -> None:
    """Drop one session's cached report (session clear / explicit invalidation)."""
    _REPORT_CACHE.pop(session_id, None)
