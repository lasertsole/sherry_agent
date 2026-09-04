import json
import hashlib
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage

DEFAULT_PROTECTED_TOOLS: set[str] = set()


def _tool_signature(tool_call: dict) -> str:
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    try:
        sorted_args = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        sorted_args = str(args)
    return f"{name}::{hashlib.md5(sorted_args.encode()).hexdigest()}"


def dedup_tool_outputs(
    messages: list[BaseMessage],
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or DEFAULT_PROTECTED_TOOLS

    sig_to_tc_ids: dict[str, list[str]] = {}
    tc_id_to_sig: dict[str, str] = {}

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                name = tc.get("name", "")
                if name in protected:
                    continue
                sig = _tool_signature(tc)
                if tc_id:
                    tc_id_to_sig[tc_id] = sig
                    sig_to_tc_ids.setdefault(sig, []).append(tc_id)

    sigs_with_dupes = {
        sig for sig, ids in sig_to_tc_ids.items() if len(ids) > 1
    }
    if not sigs_with_dupes:
        return messages, 0

    keep_tc_ids: set[str] = set()
    for sig in sigs_with_dupes:
        keep_tc_ids.add(sig_to_tc_ids[sig][-1])

    to_replace: dict[int, str] = {}
    tokens_reduced = 0

    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        sig = tc_id_to_sig.get(tc_id)
        if sig is None or sig not in sigs_with_dupes:
            continue
        if tc_id in keep_tc_ids:
            continue
        old_len = len(str(getattr(msg, "content", "")))
        tool_name = sig.split("::")[0]
        placeholder = f"[Duplicated call to {tool_name} - output cleared, see latest result]"
        new_len = len(placeholder)
        if old_len > new_len:
            tokens_reduced += (old_len - new_len) // 4
        to_replace[i] = placeholder

    if not to_replace:
        return messages, 0

    result = list(messages)
    for idx, placeholder in to_replace.items():
        result[idx] = result[idx].model_copy(update={"content": placeholder})

    return result, tokens_reduced
