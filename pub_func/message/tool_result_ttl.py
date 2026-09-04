"""Tool-result TTL + compression-budget truncation (in-place, pairing-safe).

Task 4 of the context-compression plan. Pure functions over the message
list plus a caller-owned first-seen registry; the middleware layer (Task 5)
wires them into the truncate route of the 4-way router.

WHY IN-PLACE ONLY (pairing invariant — highest-risk constraint here):
``ToolCallNormalize.before_model`` runs ``sanitize_tool_use_result_pairing``
BEFORE the compression pipeline every model call, and that sanitizer DROPS
ToolMessages with empty content. Removing a ToolMessage (or emptying its
content) therefore produces broken tool-call/tool-result pairing that hits
the provider on the SAME call. Consequently this module:

- never removes, reorders or pops anything from the message list — the only
  mutation is ``msg.content = ...`` (str path) or in-place mutation of the
  content list's blocks (multimodal path);
- always inserts a NON-EMPTY placeholder, so truncated ToolMessages survive
  the pairing sanitizer.

Timestamps: LangChain messages carry none (only the SQLite store layer
does), so first-seen times are tracked lazily in a dict keyed by
tool_call_id. The registry is volatile across restarts — accepted by design.

Coexistence note: ``target_truncation.py`` owns head/tail truncation for the
non-LLM pipeline's target budget; this module owns the TTL-expiry and
compression-budget truncation paths. Both use the same 30%/30% head/tail
ratios (CONTENT_HEAD_RATIO / CONTENT_TAIL_RATIO) and a non-empty omission
placeholder.
"""

from config.num import (
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    TTL_REGISTRY_MAX_ENTRIES,
)
from langchain_core.messages import BaseMessage, ToolMessage

from pub_func.message.estimate_msg_tokens import estimate_msg_tokens

# Non-empty by design: sanitize_tool_use_result_pairing drops ToolMessages
# whose content is empty, which would break tool pairing immediately.
TTL_PLACEHOLDER = (
    "\n\n[...truncated by context compression, "
    "tool result expired (TTL=300s)...]\n\n"
)
NON_TEXT_BLOCK_PLACEHOLDER = "[non-text block truncated by context compression]"


def record_first_seen(
    registry: dict, messages: list[BaseMessage], now: float
) -> None:
    """Record wall-clock first-seen for every tool_call_id new to registry.

    Existing entries are never overwritten (their first-seen time is the
    whole point). When writing the new ids would push the registry past
    TTL_REGISTRY_MAX_ENTRIES, the oldest entries (smallest first-seen
    value) are evicted first, then the new ids are written.

    Mutates ``registry`` in place; returns None.
    """
    new_ids: list[str] = []
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        if not tc_id or tc_id in registry:
            continue
        new_ids.append(tc_id)

    if not new_ids:
        return

    overflow = len(new_ids) - (TTL_REGISTRY_MAX_ENTRIES - len(registry))
    if overflow > 0:
        evict_count = min(overflow, len(registry))
        oldest = sorted(registry.items(), key=lambda kv: kv[1])[:evict_count]
        for key, _first_seen in oldest:
            registry.pop(key, None)

    for tc_id in new_ids:
        registry[tc_id] = now


def select_expired(
    registry: dict,
    messages: list[BaseMessage],
    ttl_seconds: float,
    now: float,
) -> list[int]:
    """Message indices whose tool result expired per TTL.

    An index qualifies when its ToolMessage's tool_call_id is registered
    with ``now - first_seen >= ttl_seconds`` and the message still exists
    in ``messages`` (ids absent from the registry are never selected —
    e.g. after a registry reset the messages simply get re-registered on
    the next ``record_first_seen`` call).
    """
    expired: list[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        if not tc_id or tc_id not in registry:
            continue
        if now - registry[tc_id] >= ttl_seconds:
            expired.append(i)
    return expired


def truncate_expired(
    messages: list[BaseMessage],
    expired_indices: list[int],
    budget_tokens: int,
) -> int:
    """In-place truncation of expired ToolMessages; returns est. tokens freed.

    Never removes or reorders messages. Indices that are out of range or do
    not point at a ToolMessage are skipped silently.
    """
    freed_total = 0
    for idx in expired_indices:
        if not isinstance(idx, int) or idx < 0 or idx >= len(messages):
            continue
        msg = messages[idx]
        if not isinstance(msg, ToolMessage):
            continue
        freed_total += _truncate_tool_message(msg, budget_tokens)
    return freed_total


def truncate_to_budget(
    messages: list[BaseMessage],
    truncatable: list[tuple[int, int]],
    budget_tokens: int,
) -> int:
    """Compression-budget truncation: largest candidates first.

    ``truncatable`` holds (message_index, est_tokens) pairs — the candidate
    format produced upstream. Candidates are sorted by estimated size
    descending and truncated in place (same head/tail + non-empty
    placeholder guarantees as :func:`truncate_expired`) until the cumulative
    freed estimate reaches ``budget_tokens`` or candidates run out.
    Returns the estimated tokens actually freed.
    """
    if budget_tokens <= 0 or not truncatable:
        return 0

    freed_total = 0
    ordered = sorted(truncatable, key=lambda pair: pair[1], reverse=True)
    for idx, _est in ordered:
        if freed_total >= budget_tokens:
            break
        if not isinstance(idx, int) or idx < 0 or idx >= len(messages):
            continue
        msg = messages[idx]
        if not isinstance(msg, ToolMessage):
            continue
        freed_total += _truncate_tool_message(msg, budget_tokens)
    return freed_total


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _est_with_content(msg: BaseMessage, content) -> int:
    """Estimate msg tokens as if its content were ``content`` (no mutation)."""
    return estimate_msg_tokens(msg.model_copy(update={"content": content}))


def _build_head_tail(text: str, head_len: int, tail_len: int, placeholder: str) -> str:
    tail_part = text[len(text) - tail_len:] if tail_len > 0 else ""
    return text[:head_len] + placeholder + tail_part


def _is_text_block(block) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _truncate_tool_message(msg: BaseMessage, budget_tokens: int) -> int:
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return _truncate_str_content(msg, budget_tokens)
    if isinstance(content, list):
        return _truncate_list_content(msg, budget_tokens)
    return 0


def _truncate_str_content(msg: ToolMessage, budget_tokens: int) -> int:
    """Head/tail-truncate a str content in place; returns est. tokens freed.

    Keeps head 30% + tail 30% around the placeholder. If the result still
    exceeds ``budget_tokens`` the tail is shrunk further (head is kept).
    No-ops when the message is too small for the placeholder overhead to
    pay off (truncating would grow it) or when nothing would be freed.
    """
    content = msg.content
    if not isinstance(content, str) or not content:
        return 0

    est_before = estimate_msg_tokens(msg)
    head_len = int(len(content) * CONTENT_HEAD_RATIO)
    tail_len = int(len(content) * CONTENT_TAIL_RATIO)

    candidate = _build_head_tail(content, head_len, tail_len, TTL_PLACEHOLDER)
    if len(candidate) >= len(content):
        return 0  # placeholder overhead would grow the content — skip

    while (
        budget_tokens is not None
        and tail_len > 0
        and _est_with_content(msg, candidate) > budget_tokens
    ):
        tail_len //= 2
        candidate = _build_head_tail(content, head_len, tail_len, TTL_PLACEHOLDER)

    est_after = _est_with_content(msg, candidate)
    freed = est_before - est_after
    if freed <= 0:
        return 0
    msg.content = candidate
    return freed


def _truncate_list_content(msg: ToolMessage, budget_tokens: int) -> int:
    """Truncate a multimodal block-list content in place.

    Text blocks get the same head/tail rule applied to their ``text`` field
    (mutated in place); non-text blocks (image etc.) are replaced by a text
    placeholder dict. The list structure is NEVER dropped: same length,
    still a list, one output block per input block.
    """
    blocks = msg.content
    if not isinstance(blocks, list) or not blocks:
        return 0

    est_before = estimate_msg_tokens(msg)

    # Pass 1: head/tail each text block; replace non-text blocks.
    for i, block in enumerate(blocks):
        if _is_text_block(block):
            text = block["text"]
            head_len = int(len(text) * CONTENT_HEAD_RATIO)
            tail_len = int(len(text) * CONTENT_TAIL_RATIO)
            candidate = _build_head_tail(text, head_len, tail_len, TTL_PLACEHOLDER)
            if len(candidate) < len(text):
                block["text"] = candidate
        else:
            blocks[i] = {"type": "text", "text": NON_TEXT_BLOCK_PLACEHOLDER}

    # Pass 2: still over budget -> shrink text-block tails (heads kept).
    while (
        budget_tokens is not None
        and _est_with_content(msg, blocks) > budget_tokens
    ):
        shrunk = False
        for block in blocks:
            if not _is_text_block(block):
                continue
            text = block["text"]
            head_len = text.find(TTL_PLACEHOLDER)
            if head_len == -1:
                continue  # never truncated; nothing to shrink around
            tail_len = len(text) - (head_len + len(TTL_PLACEHOLDER))
            if tail_len <= 0:
                continue
            block["text"] = _build_head_tail(
                text, head_len, tail_len // 2, TTL_PLACEHOLDER
            )
            shrunk = True
        if not shrunk:
            break  # every tail already collapsed; heads are kept

    freed = est_before - estimate_msg_tokens(msg)
    return max(freed, 0)
