"""Digest folding for the compression-time memory review.

The memory review reads a conversation on its way out of the window, and the
conversation at a compaction point is close to the model's own budget
(``window × 0.8``). Handing it the raw history is how the review came within a
few thousand tokens of the window itself; folding everything older than the
recent tail into one digest line per message keeps the pass at summary level — which
is all it needs, because its job is "extract facts worth remembering", not
"answer about the transcript".

Shape (mirrors hermes-agent's ``_digest_history``):

- the last :data:`DIGEST_TAIL` messages are passed through verbatim — the most
  recent exchanges carry the still-open threads;
- everything older becomes ONE ``HumanMessage`` listing ``ROLE: text`` lines,
  each truncated to a per-role budget (user 300 / assistant 200 / tool 150);
- a history that already fits the tail budget is returned unchanged, so short
  sessions pay nothing.

Multimodal content is handled by :func:`_text_of`, which keeps the text blocks
and drops image/audio blocks: the review has the facts it needs in the text, and
a base64 image block would blow the budget it is trying to protect.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

__all__ = [
    "DIGEST_ASSISTANT_MAX",
    "DIGEST_TAIL",
    "DIGEST_TOOL_MAX",
    "DIGEST_USER_MAX",
    "digest_history",
]

#: Messages at the end of the history that are passed through verbatim.
DIGEST_TAIL = 24

#: Per-message character caps inside the digest (by role).
DIGEST_USER_MAX = 300
DIGEST_ASSISTANT_MAX = 200
DIGEST_TOOL_MAX = 150

_DIGEST_HEADER = (
    "[Earlier conversation digest — older turns summarized to save context. "
    "{count} messages condensed.]"
)


def digest_history(messages: list[BaseMessage], tail: int = DIGEST_TAIL) -> list[BaseMessage]:
    """Fold everything older than *tail* into one digest line per message.

    Returns a message list that can be handed straight to the review agent. The
    order is preserved: digest first, then the verbatim tail.
    """
    if tail < 0:
        tail = 0
    if len(messages) <= tail:
        return list(messages)

    # ``tail == 0`` means "fold everything": ``messages[-0:]`` would be the WHOLE
    # list (Python's -0 is 0), which kept the history verbatim in the one case
    # that needs the fold most.
    older = messages if tail == 0 else messages[: len(messages) - tail]
    recent = [] if tail == 0 else list(messages[-tail:])

    lines: list[str] = []
    for message in older:
        if isinstance(message, ToolMessage):
            role, limit = "TOOL", DIGEST_TOOL_MAX
        elif isinstance(message, AIMessage):
            role, limit = "ASSISTANT", DIGEST_ASSISTANT_MAX
        elif isinstance(message, HumanMessage):
            role, limit = "USER", DIGEST_USER_MAX
        else:  # SystemMessage and anything else: not conversation content
            continue

        content = _text_of(message).strip()
        if not content:
            continue
        if len(content) > limit:
            content = content[:limit] + "..."
        lines.append(f"{role}: {content}")

    if not lines:
        return recent

    digest = _DIGEST_HEADER.format(count=len(older)) + "\n\n" + "\n".join(lines)
    return [HumanMessage(content=digest), *recent]


def _text_of(message: BaseMessage) -> str:
    """The text content of *message*, whether it is a string or a block list."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content)
