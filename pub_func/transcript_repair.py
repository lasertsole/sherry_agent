"""
Transcript Repair

Tool use/result pairing repair for assembled context.

Repair tool_use/toolResult pairing after message trimming to prevent agent "Message ordering conflict" errors
"""

import time
from typing import TypedDict, Any
from langchain_core.messages import ToolMessage, AIMessage, BaseMessage

class ToolCallLike(TypedDict):
    """Tool-call type definition."""
    id: str
    name: str | None
    error: str | None


def extract_tool_call_id(block: dict[str, Any]) -> str | None:
    """
    Extract the ID from a tool-call block.

    Args:
        block: Tool-call dictionary.

    Returns:
        Tool-call ID, or None if not found.
    """
    if isinstance(block.get('id'), str) and block['id']:
        return block['id']

    if isinstance(block.get('call_id'), str) and block['call_id']:
        return block['call_id']

    return None


def extract_tool_calls_from_assistant(msg: AIMessage) -> list[ToolCallLike]:
    """
    Extract tool-call entries from an assistant message.

    Args:
        msg: Assistant message.

    Returns:
        List of tool-call entries.
    """
    tool_calls = getattr(msg, "tool_calls", None)

    if not isinstance(tool_calls, list):
        return []

    calls: list[ToolCallLike] = []

    for block in tool_calls:
        if not block or not isinstance(block, dict):
            continue

        call_id = extract_tool_call_id(block)

        if not call_id:
            continue

        block_type = block.get('type')

        if isinstance(block_type, str) and block_type == "tool_call":
            calls.append(ToolCallLike(id = call_id, name = block.get('name') if isinstance(block.get('name'), str) else None, error=None))

    return calls

def extract_invalid_tool_calls_from_assistant(msg: AIMessage) -> list[ToolCallLike]:
    """
    Extract invalid tool-call entries from an assistant message.

    Args:
        msg: Assistant message.

    Returns:
        List of invalid tool-call entries.
    """
    invalid_tool_calls = getattr(msg, "invalid_tool_calls", None)

    if not isinstance(invalid_tool_calls, list):
        return []

    calls: list[ToolCallLike] = []

    for block in invalid_tool_calls:
        if not block or not isinstance(block, dict):
            continue

        call_id = extract_tool_call_id(block)

        if not call_id:
            continue

        error = block.get('error', '')

        if isinstance(error, str) and error != "":
            calls.append({
                'id': call_id,
                'name': block.get('name') if isinstance(block.get('name'), str) else None,
                'error': error,
            })

    return calls


def extract_tool_result_id(msg: ToolMessage) -> str | None:
    """
    Extract the tool-call ID from a tool-result message.

    Args:
        msg: Tool-result message.

    Returns:
        Tool-call ID, or None if not found.
    """
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id and isinstance(tool_call_id, str):
        return tool_call_id

    return None


def make_missing_tool_result(tool_call_id: str, tool_name: str | None = None) -> ToolMessage:
    """
    Create a placeholder tool-result message for a missing result.

    Args:
        tool_call_id: Tool-call ID.
        tool_name: Tool name (optional).

    Returns:
        A dummy ToolMessage indicating the result was missing.
    """
    return ToolMessage(
        name = tool_name or 'unknown',
        content = "tool result missing after context trim.",
        tool_call_id = tool_call_id,
        status = "error",
        additional_kwargs = {"timestamp": int(time.time() * 1000)}
    )


def sanitize_tool_use_result_pairing(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Repair tool-call / tool-result pairing.

    Fixes tool_use/tool_result pairing after message trimming to prevent
    "Message ordering conflict" errors from the agent.

    Args:
        messages: List of messages.

    Returns:
        Repaired message list.
    """
    out: list[BaseMessage] = []
    seen_tool_result_ids: set[str] = set()
    changed = False

    def push_tool_result(msg: ToolMessage) -> None:
        """Push a tool-result message, deduplicating by ID."""
        nonlocal changed

        result_id = extract_tool_result_id(msg)

        if result_id and result_id in seen_tool_result_ids:
            changed = True
            return

        if result_id:
            seen_tool_result_ids.add(result_id)

        out.append(msg)

    i = 0
    while i < len(messages):
        msg = messages[i]

        if not isinstance(msg, AIMessage):
            # Keep ToolMessages with content; drop empty ones
            if isinstance(msg, ToolMessage):
                if msg.content:
                    out.append(msg)
                else:
                    changed = True
            else:
                out.append(msg)

            i += 1
            continue

        # Keep error-status messages (but clear invalid_tool_calls to prevent serialisation as OpenAI tool_calls)
        raw_invalid_tool_calls = getattr(msg, "invalid_tool_calls", None)
        has_invalid = isinstance(raw_invalid_tool_calls, list) and len(raw_invalid_tool_calls) > 0
        invalid_tool_calls: list[ToolCallLike] = extract_invalid_tool_calls_from_assistant(msg)

        if getattr(msg, 'status', "") == "error" or has_invalid:
            if has_invalid:
                # LangChain serialises invalid_tool_calls as tool_calls for the API;
                # if a corresponding ToolMessage is missing this triggers a 400 error.
                # Historical invalid calls are simply cleared.
                msg = msg.model_copy(update={"invalid_tool_calls": []})
                changed = True
            out.append(msg)
            i += 1
            continue

        # Extract tool calls
        tool_calls = extract_tool_calls_from_assistant(msg)

        if not tool_calls:
            out.append(msg)
            i += 1
            continue

        tool_call_ids:set[str] = {t['id'] for t in tool_calls}
        span_results_by_id: dict[str, ToolMessage] = {}
        remainder: list[ToolMessage] = []

        # Find subsequent tool results
        j = i + 1
        while j < len(messages):
            next_msg = messages[j]

            if isinstance(next_msg, AIMessage):
                break

            if isinstance(next_msg, ToolMessage) and next_msg.content:
                result_id = extract_tool_result_id(next_msg)

                if result_id and result_id in tool_call_ids:
                    if result_id in seen_tool_result_ids:
                        changed = True
                        j += 1
                        continue

                    if result_id not in span_results_by_id:
                        span_results_by_id[result_id] = next_msg

                    j += 1
                    continue

            if isinstance(next_msg, ToolMessage) and getattr(next_msg, "content", "") != "":
                remainder.append(next_msg)
            else:
                changed = True

            j += 1

        # Add assistant message
        out.append(msg)

        if len(span_results_by_id)>0 and len(remainder)>0:
            changed = True

        # Add tool results (existing or dummy)
        for call in tool_calls:
            existing = span_results_by_id.get(call['id'])

            if existing:
                push_tool_result(existing)
            else:
                changed = True
                push_tool_result(make_missing_tool_result(
                    tool_call_id=call['id'],
                    tool_name=call.get('name'),
                ))

        # Add remaining messages
        for rem in remainder:
            out.append(rem)

        i = j

    # Final cleanup: remove orphaned ToolMessages (no corresponding AIMessage.tool_calls)
    cleaned: list[BaseMessage] = []
    active_tool_call_ids: set[str] = set()

    for msg in out:
        if isinstance(msg, AIMessage):
            calls = extract_tool_calls_from_assistant(msg)
            active_tool_call_ids = {c['id'] for c in calls}

        if isinstance(msg, ToolMessage):
            result_id = extract_tool_result_id(msg)
            if result_id and result_id not in active_tool_call_ids:
                changed = True
                continue

        cleaned.append(msg)

    return cleaned if changed else messages
