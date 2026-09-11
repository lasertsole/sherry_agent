"""Tool-call args truncation (head+tail with middle omission).

Truncates oversized ``AIMessage.tool_calls[].args`` — the compression
pipeline previously only handled ``ToolMessage`` content, so huge tool
arguments (large ``write_file`` payloads, long bash scripts) stayed in
the transcript forever.

Replacement format: ``{"_truncated_args": "head...[args truncated,
omitted N chars]...tail"}`` — keeps ``args`` a dict (LangChain
``ToolCall.args`` type), stays JSON-serializable for every provider
adapter, and lets the model clearly see the args were cut.

Style parity:
  - ``target_truncation.py``: returns ``(new_list, freed_tokens)``;
    head/tail via ``CONTENT_HEAD_RATIO`` / ``CONTENT_TAIL_RATIO``.
  - ``dedup_tool_outputs``: ``protected_tools`` parameter.
  - ``find_truncatable_tool_results``: ``skip_recent`` (most recent
    messages never touched).
"""

import json
from config.features import SUMMARIZATION
from langchain_core.messages import BaseMessage, AIMessage

MAX_TOOL_ARGS_CHARS = SUMMARIZATION["max_tool_args_chars"]
MIN_ARGS_CHARS_TO_TRUNCATE = SUMMARIZATION["min_args_chars_to_truncate"]
CONTENT_HEAD_RATIO = SUMMARIZATION["content_head_ratio"]
CONTENT_TAIL_RATIO = SUMMARIZATION["content_tail_ratio"]
TRUNCATABLE_RECENT_SKIP = SUMMARIZATION["truncatable_recent_skip"]

_ARGS_OMISSION_TEMPLATE = "...[args truncated, omitted {omitted} chars]..."


def _truncate_args_str(
    args_str: str,
    max_chars: int,
    head_ratio: float,
    tail_ratio: float,
) -> str:
    head = args_str[: int(max_chars * head_ratio)]
    tail = args_str[-int(max_chars * tail_ratio) :]
    omitted = len(args_str) - len(head) - len(tail)
    return f"{head}{_ARGS_OMISSION_TEMPLATE.format(omitted=omitted)}{tail}"


def truncate_tool_args(
    messages: list[BaseMessage],
    max_args_chars: int = MAX_TOOL_ARGS_CHARS,
    min_args_chars: int = MIN_ARGS_CHARS_TO_TRUNCATE,
    protected_tools: set[str] | None = None,
    skip_recent: int = TRUNCATABLE_RECENT_SKIP,
    head_ratio: float = CONTENT_HEAD_RATIO,
    tail_ratio: float = CONTENT_TAIL_RATIO,
) -> tuple[list[BaseMessage], int]:
    """Truncate oversized tool-call args in AIMessages (pure — no mutation).

    Walks every ``AIMessage`` before the last ``skip_recent`` messages;
    any tool_call whose serialized args exceed ``min_args_chars`` and is
    not in ``protected_tools`` gets its args replaced with a head+tail
    truncation capped at ``max_args_chars``. Original messages are never
    modified — replaced AIMessages are ``model_copy`` clones, so
    AIMessage <-> ToolMessage pairing (tool_call_id) stays intact.

    Returns ``(new_messages, freed_tokens)`` where ``freed_tokens`` is
    the estimated token saving (chars // 4, same estimator convention
    as ``target_truncate_tool_outputs``).
    """
    protected = protected_tools or set()
    keep_until = max(len(messages) - skip_recent, 0)

    result = list(messages)
    freed_total = 0

    for i in range(keep_until):
        msg = result[i]
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue

        modified = False
        new_tcs = []
        for tc in tool_calls:
            name = tc.get("name", "")
            if name in protected:
                new_tcs.append(tc)
                continue

            args = tc.get("args", {})
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = str(args)

            if len(args_str) <= min_args_chars:
                new_tcs.append(tc)
                continue

            truncated = _truncate_args_str(args_str, max_args_chars, head_ratio, tail_ratio)
            new_args = {"_truncated_args": truncated}
            new_tcs.append({**tc, "args": new_args})

            new_args_str = json.dumps(new_args, ensure_ascii=False)
            freed_total += max((len(args_str) - len(new_args_str)) // 4, 0)
            modified = True

        if modified:
            result[i] = msg.model_copy(update={"tool_calls": new_tcs})

    return result, freed_total
