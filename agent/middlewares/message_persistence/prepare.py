"""Batch preparation primitives shared by the persistence middleware.

Moved verbatim from ``agent/middlewares/summarization/compaction_persistence.py``
when persistence left the compression path: the helpers below are not
compression-specific, they classify and repair a batch of graph-state messages
before it is handed to the MesMemory writer.

- :func:`_is_persistable` — role filter (human/ai/tool only), minus messages
  already flushed in this process and minus compression artifacts.
- :func:`_watermark_key` — the stable per-message watermark key: the LangGraph
  message id, or a content triple hash when a message carries no id.
- :func:`_reconcile_denials_for_persistence` — re-attaches HITL-denied tool
  calls (stripped by ``HumanInTheLoop.after_model``) so the denial ToolMessage
  keeps a paired AI row.
- :func:`_dedup_tool_results` — drops empty and duplicate-id tool results at
  the persistence level (deliberately narrower than the transcript sanitizer).

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
from typing import Any

from context_engine import is_message_persisted
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


def _fingerprint_key(message: BaseMessage) -> str:
    """Content-triple fingerprint: id-less messages with equal payload match."""
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


def _watermark_key(message: BaseMessage) -> str:
    """Primary watermark key: the LangGraph message id, or the fingerprint."""
    message_id = getattr(message, "id", None)
    if isinstance(message_id, str) and message_id:
        return message_id
    return _fingerprint_key(message)


def _watermark_lookup_keys(message: BaseMessage) -> list[str]:
    """Keys a persisted watermark may hold for ``message``, primary first.

    The wrap hook persists a tool result the moment it returns — BEFORE the
    graph reducer assigns the message an id — so the tombstone it writes is
    the content fingerprint. The next boundary sees the same object WITH an
    id, and after a restart only the id survives: looking up just the id
    would re-persist the row. Checking both keys closes that hole while
    marking still records the primary one.
    """
    primary = _watermark_key(message)
    fingerprint = _fingerprint_key(message)
    if primary == fingerprint:
        return [primary]
    return [primary, fingerprint]


def _dedup_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drop empty and duplicate-id tool results (persistence-level dedup).

    Deliberately narrower than ``sanitize_tool_use_result_pairing``: that
    function rebuilds an LLM-valid transcript and consumes any message between
    an AI tool call and the next AI message, which would silently delete
    legitimate user turns from a persisted batch.
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


__all__ = [
    "_dedup_tool_results",
    "_fingerprint_key",
    "_is_persistable",
    "_reconcile_denials_for_persistence",
    "_watermark_key",
    "_watermark_lookup_keys",
]
