"""Child handoff parsing and result-text extraction.

The child's answer carries a ```json block with five EXPERIENCE fields about
its own layer; :func:`_extract_handoff_json` parses the LAST such block (a
missing/malformed one fails open to ``None``, and the caller falls back to the
free-text result). :func:`_extract_result_text` walks a child's final state
backwards to the last substantive assistant text.

Kept apart from the spawn pipeline so the handoff contract (and the legacy
``failed_paths`` → ``failed_branches`` normalization) has one home.
"""

import json
import re


#: The five EXPERIENCE fields a child reports about ITS OWN layer. Metadata
#: (depth/label/children/outcome) is code-assembled later — a model that echoes
#: them back is stripped here so layers cannot smuggle each other's data.
_HANDOFF_FIELDS = (
    "task_understanding",
    "discovered_hard_constraints",
    "locked_decisions",
    "failed_branches",
    "success_steps",
)
_HANDOFF_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _normalise_handoff(data: dict) -> dict:
    """Project one parsed handoff, accepting the pre-rename ``failed_paths`` key."""
    result = {key: data[key] for key in _HANDOFF_FIELDS if key in data}
    if "failed_branches" not in result:
        legacy = data.get("failed_paths")
        if isinstance(legacy, list):
            result["failed_branches"] = [
                {
                    "branch": str(item.get("branch") or item.get("path") or ""),
                    "failure_reason": str(item.get("failure_reason") or ""),
                }
                for item in legacy
                if isinstance(item, dict)
            ]
    elif isinstance(result.get("failed_branches"), list):
        result["failed_branches"] = [
            {
                "branch": str(item.get("branch") or item.get("path") or ""),
                "failure_reason": str(item.get("failure_reason") or ""),
            }
            for item in result["failed_branches"]
            if isinstance(item, dict)
        ]
    return result


def _extract_handoff_json(result_text: str | None) -> dict | None:
    """The child's handoff JSON block, or None (fail-open).

    Phase 1 is prompt-bound: the child appends a ```json block with five
    experience fields to its Task Result. The LAST parseable block carrying at
    least one of them wins; depth/agent_label/outcome/children are dropped here
    (program-injected later). A missing/malformed block returns None, and the
    plan extractor falls back to the free-text result. An older child that still
    emitted ``failed_paths`` is normalized to ``failed_branches``.
    """
    if not result_text:
        return None
    for raw in reversed(_HANDOFF_JSON_BLOCK.findall(result_text)):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if not any(key in data for key in (*_HANDOFF_FIELDS, "failed_paths")):
            continue
        return _normalise_handoff(data)
    return None


def _extract_result_text(agent_result: dict[str, object] | None) -> str | None:
    """Extract the final assistant answer from a child agent's invocation result.

    The last message in the returned state is frequently an ``AIMessage`` with
    empty content (containing only ``tool_calls``) or a ``ToolMessage`` — both
    carry tool output rather than the subagent's final answer. This walks the
    message list *backwards* to the last message that contains substantive text,
    flattening multimodal content-block lists into plain text.

    Args:
        agent_result: The raw result dict returned by ``CompiledStateGraph.ainvoke``.

    Returns:
        The extracted result text, or ``None`` if no substantive text is found.
    """
    if not agent_result or not isinstance(agent_result, dict):
        return None

    messages = agent_result.get("messages") or []
    if not messages:
        return None

    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        if isinstance(content, list):
            # Multimodal content-block list — extract and concat text items
            texts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    texts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text", "")
                    if str(t).strip():
                        texts.append(str(t))
            if texts:
                return "\n".join(texts)
            continue
        # Fallback: any other non-empty content is treated as result text
        text = str(content).strip()
        if text:
            return text

    return None
