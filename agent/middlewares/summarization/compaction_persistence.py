"""Pre-replacement persistence of the compaction-discarded prefix.

The summarization middleware runs compaction *inside* ``wrap_model_call``: T2/T3
only override the outgoing request, T1 additionally writes the compacted list
back to graph state. This module flushes the messages that are actually being
removed — before the summary pair replaces them — so MesMemory keeps the raw
conversation even though the model now sees the summary.

Three guarantees:

1. **Original content** — the flush receives ``original_messages``
   (``request.state["messages"]``), never the post-``_run_non_llm_strategies``
   copies: dedup, pruning and truncation deliberately rewrite tool output
   content and drop messages, and persisting those copies would silently
   corrupt the raw store.
2. **Prefix exactness** — the discarded slice is located by object identity
   (``preserved[0]`` found in ``original_messages`` via ``id()``); when a
   non-LLM strategy replaced that object, the fallback keeps every original
   whose ``id()`` is absent from ``preserved``.
3. **Write-once** — messages carrying the in-process ``_db_persisted`` marker
   and messages below the persistent watermark (``persisted_message_ids``) are
   skipped, so a T2 flush followed by a T1 flush of the same slice, or a
   replay after a process restart (markers lost with the deserialized
   checkpoint), never inserts a duplicate row.

The batch is NOT run through ``sanitize_tool_use_result_pairing``: that
sanitizer rewrites the transcript for LLM input (it consumes every message
between an AI tool call and the next AI message — including user turns — and
inserts missing-result placeholders), which would drop legitimate messages from
the raw store. The store does not need valid pairing; only duplicate and empty
tool results are filtered here, plus the HITL denial repair so denied calls keep
their paired AI row.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from loguru import logger

from context_engine import (
    add_messages,
    add_messages_sync,
    filter_persisted_message_ids,
    is_message_persisted,
    mark_message_ids_persisted,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

_SUMMARY_LC_SOURCE = "summarization"


def _reconcile_denials_for_persistence(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Re-attach denied tool calls so HITL denials survive persistence.

    ``HumanInTheLoop.after_model`` strips rejected tool calls from the AIMessage
    and appends the denial ``ToolMessage`` afterwards, which leaves the denial
    orphaned (no AIMessage carries its ``tool_call_id`` anymore).
    ``sanitize_tool_use_result_pairing`` drops orphaned ToolMessages, so the
    rejection would be lost from MesMemory history.

    This helper runs on the persistence slice only (never on graph state): for
    every error-status ToolMessage whose ``tool_call_id`` no longer appears in
    any AIMessage, the call is re-attached to the closest preceding AIMessage
    (args are unrecoverable after the strip, so ``{}`` is used). The restored
    pair then survives sanitization and is persisted as a normal tool row.
    """
    orphan_ids: set[str] = set()
    for msg in messages:
        if (
            isinstance(msg, ToolMessage)
            and getattr(msg, "status", "") == "error"
            and getattr(msg, "content", "")
        ):
            tc_id = getattr(msg, "tool_call_id", None)
            if isinstance(tc_id, str) and tc_id:
                orphan_ids.add(tc_id)

    if not orphan_ids:
        return messages

    for msg in messages:
        if isinstance(msg, AIMessage):
            for call in getattr(msg, "tool_calls", None) or []:
                call_id = call.get("id") if isinstance(call, dict) else None
                if isinstance(call_id, str):
                    orphan_ids.discard(call_id)

    if not orphan_ids:
        return messages

    out: list[BaseMessage] = []
    n = len(messages)
    for idx, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            # Peek at the following ToolMessage run (until the next AIMessage)
            # WITHOUT consuming it — every message is still appended below.
            attached: list[dict[str, Any]] = []
            j = idx + 1
            while j < n and not isinstance(messages[j], AIMessage):
                t = messages[j]
                if isinstance(t, ToolMessage):
                    tc_id = getattr(t, "tool_call_id", None)
                    if isinstance(tc_id, str) and tc_id in orphan_ids:
                        attached.append(
                            {
                                "name": getattr(t, "name", None) or "unknown",
                                "args": {},
                                "id": tc_id,
                                "type": "tool_call",
                            }
                        )
                j += 1

            if attached:
                existing = [
                    c for c in (getattr(msg, "tool_calls", None) or []) if isinstance(c, dict)
                ]
                existing_ids = {c.get("id") for c in existing}
                merged = existing + [c for c in attached if c["id"] not in existing_ids]
                msg = msg.model_copy(update={"tool_calls": merged})
                for c in attached:
                    orphan_ids.discard(c["id"])

        out.append(msg)
    return out


def _select_discarded_originals(
    original_messages: Sequence[BaseMessage], preserved: Sequence[BaseMessage]
) -> list[BaseMessage]:
    """Return the original objects the compression removes from graph state.

    Primary path: ``preserved[0]`` (the first post-strategy message kept) is
    found in ``original_messages`` by identity; everything before it is
    discarded. Fallback: a strategy replaced that object, so keep every
    original whose ``id()`` is not among the preserved objects.
    """
    if not original_messages:
        return []
    if not preserved:
        return list(original_messages)
    first_preserved = preserved[0]
    for index, message in enumerate(original_messages):
        if message is first_preserved:
            return list(original_messages[:index])
    preserved_objects = {id(message) for message in preserved}
    return [message for message in original_messages if id(message) not in preserved_objects]


def _is_persistable(message: Any) -> bool:
    if getattr(message, "type", None) not in ("human", "ai", "tool"):
        return False
    if is_message_persisted(message):
        return False
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    # Compression artifacts (the summary pair and any previously chained
    # summary) never belong in the raw store — the row builder filters the
    # human half for the same reason; the AI half is filtered here.
    return additional_kwargs.get("lc_source") != _SUMMARY_LC_SOURCE


def _watermark_key(message: BaseMessage) -> str:
    message_id = getattr(message, "id", None)
    if isinstance(message_id, str) and message_id:
        return message_id
    # Defensive fallback for messages with no LangGraph-assigned id: the same
    # content triple is treated as the same message.
    payload = json.dumps(
        {
            "role": message.type,
            "content": getattr(message, "content", None),
            "tool_call_id": getattr(message, "tool_call_id", None),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _fresh_candidates(
    session_id: str,
    original_messages: Sequence[BaseMessage],
    preserved: Sequence[BaseMessage],
) -> list[BaseMessage]:
    discarded = _select_discarded_originals(original_messages, preserved)
    candidates = [message for message in discarded if _is_persistable(message)]
    if not candidates:
        return []
    keys = [_watermark_key(message) for message in candidates]
    already_persisted = filter_persisted_message_ids(session_id, keys)
    return [
        message
        for message, key in zip(candidates, keys, strict=True)
        if key not in already_persisted
    ]


def _dedup_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drop empty and duplicate-id tool results (persistence-level dedup).

    Deliberately narrower than ``sanitize_tool_use_result_pairing``: that
    function rebuilds an LLM-valid transcript and consumes any message between
    an AI tool call and the next AI message, which would silently delete
    legitimate user turns from a multi-turn discarded prefix.
    """
    out: list[BaseMessage] = []
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            result_id = getattr(message, "tool_call_id", None)
            if not getattr(message, "content", ""):
                continue
            if isinstance(result_id, str) and result_id:
                if result_id in seen:
                    continue
                seen.add(result_id)
        out.append(message)
    return out


def _mark_written(session_id: str, attempted: Sequence[BaseMessage]) -> None:
    # Mark every candidate that was handed to the writer, not only the rows the
    # writer kept: a deduplicated copy must not resurface as a fresh candidate
    # on the next compression.
    mark_message_ids_persisted(session_id, [_watermark_key(message) for message in attempted])


def persist_discarded_messages_sync(
    session_id: str,
    original_messages: Sequence[BaseMessage],
    preserved: Sequence[BaseMessage],
) -> list[BaseMessage]:
    """Synchronous flush for the sync compaction path; returns the written batch."""
    candidates = _fresh_candidates(session_id, original_messages, preserved)
    if not candidates:
        return []
    prepared = _dedup_tool_results(_reconcile_denials_for_persistence(candidates))
    if not prepared:
        return []
    add_messages_sync(session_id, prepared)
    _mark_written(session_id, candidates)
    logger.debug(
        "compaction persistence: wrote {} messages for session {}",
        len(prepared),
        session_id,
    )
    return prepared


async def persist_discarded_messages(
    session_id: str,
    original_messages: Sequence[BaseMessage],
    preserved: Sequence[BaseMessage],
) -> list[BaseMessage]:
    """Async twin of :func:`persist_discarded_messages_sync`."""
    candidates = _fresh_candidates(session_id, original_messages, preserved)
    if not candidates:
        return []
    prepared = _dedup_tool_results(_reconcile_denials_for_persistence(candidates))
    if not prepared:
        return []
    await add_messages(session_id, prepared)
    _mark_written(session_id, candidates)
    logger.debug(
        "compaction persistence: wrote {} messages for session {}",
        len(prepared),
        session_id,
    )
    return prepared
