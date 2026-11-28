"""Discussion-history compaction for meetings (plan §4, 阶段 3).

The plan reuses the summarization middleware's compression of arbitrary
``AnyMessage`` lists, but with a MEETING-OWNED trigger seam — it must not invoke
the main agent's task-intent / system-prompt re-arming (plan §13.3 kill-criteria
note). We therefore call the reusable summary primitives directly:

* :func:`_build_static_fallback_summary` — deterministic, no model;
* ``Summarization._create_summary`` / ``_acreate_summary`` — the real LLM path,
  injected as a callable so tests never build a model.

Compressed segments are persisted in ``meeting_compaction_segments`` (already in
the phase-1 schema) and the summary is appended as a ``summary``-role message.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agent.meeting.models import MeetingMessage, SpeakerKind
from agent.meeting.persistence import MeetingStore

__all__ = [
    "CompactorFn",
    "DEFAULT_KEEP_RECENT",
    "compact_history",
    "maybe_compact",
    "static_compactor",
]


#: Keep this many newest utterances verbatim; older ones are folded into a
#: summary segment. Small enough to bound context, large enough for continuity.
DEFAULT_KEEP_RECENT = 12
COMPACT_THRESHOLD = 24


class CompactorFn(Protocol):
    """Injectable compactor: a list of LangChain messages → a summary string."""

    def __call__(self, messages: list[AnyMessage]) -> str: ...


def _meeting_to_langchain(messages: list[dict[str, Any]]) -> list[AnyMessage]:
    """Map stored meeting message rows to Human/AI messages for summarization.

    Host and participant utterances become AI messages; the user becomes Human.
    Speaker names are prefixed so the summary keeps track of who said what.
    """
    out: list[AnyMessage] = []
    for row in messages:
        speaker = row.get("speaker_name") or row.get("speaker_id") or "?"
        text = f"[{speaker}]: {row.get('content', '')}"
        if row.get("speaker_kind") == SpeakerKind.USER.value:
            out.append(HumanMessage(content=text))
        else:
            out.append(AIMessage(content=text))
    return out


def static_compactor(messages: list[AnyMessage]) -> str:
    """Deterministic compactor reusing summarization's static fallback."""
    from agent.middlewares.summarization.summary_generation import (
        _build_static_fallback_summary,
    )

    return _build_static_fallback_summary(messages)


def make_llm_compactor(summarizer: Any) -> CompactorFn:
    """Adapt a ``Summarization`` instance's ``_create_summary`` to CompactorFn."""

    def compact(messages: list[AnyMessage]) -> str:
        result = summarizer._create_summary(messages, session_id="")
        if hasattr(result, "markdown"):
            return str(result.markdown)
        return str(result)

    return compact


async def compact_history(
    store: MeetingStore,
    meeting_id: str,
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    compactor: CompactorFn | None = None,
    threshold: int = COMPACT_THRESHOLD,
) -> dict[str, Any]:
    """Compact a meeting's oldest utterances into a persisted summary segment.

    No-op (``compacted=False``) when the message count is below ``threshold``.
    Otherwise the oldest ``len(messages) - keep_recent`` rows are summarized,
    a segment row is inserted, and a ``summary``-role message is appended so the
    visible history reflects the compaction. Returns the segment summary plus
    the untouched recent rows.
    """
    rows = await store.list_messages(meeting_id)
    if len(rows) <= threshold:
        return {"compacted": False, "message_count": len(rows)}

    cutoff = len(rows) - keep_recent
    old_rows, recent_rows = rows[:cutoff], rows[cutoff:]
    to_compact = _meeting_to_langchain(old_rows)
    compact = compactor or static_compactor
    summary = compact(to_compact)

    await store.append_compaction_segment(
        meeting_id,
        summary=summary,
        start_sequence=int(old_rows[0]["sequence"]),
        end_sequence=int(old_rows[-1]["sequence"]),
    )
    summary_message = MeetingMessage(
        meeting_id=meeting_id,
        sequence=await store.next_sequence(meeting_id),
        speaker_kind=SpeakerKind.HOST.value,
        speaker_id="host",
        speaker_name="Host",
        message_role="summary",
        content=summary,
        timestamp=int(time.time()),
    )
    await store.append_message(summary_message)
    return {
        "compacted": True,
        "message_count": len(rows),
        "compacted_count": len(old_rows),
        "summary": summary,
        "recent": recent_rows,
    }


async def maybe_compact(store: MeetingStore, meeting_id: str, **kwargs: Any) -> dict[str, Any]:
    """Convenience alias for :func:`compact_history` used by the loop/boot paths."""
    return await compact_history(store, meeting_id, **kwargs)
