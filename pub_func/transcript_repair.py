"""
Transcript Repair

Tool use/result pairing repair for assembled context.

裁剪消息后修复 tool_use/toolResult 配对，防止 agent 报 "Message ordering conflict"
"""

import time
from typing import TypedDict, List, Set, Dict, Any, Optional
from langchain_core.messages import ToolMessage, AIMessage, BaseMessage

class ToolCallLike(TypedDict):
    """工具调用类型"""
    id: str
    name: Optional[str]
    error: Optional[str]


def extract_tool_call_id(block: Dict[str, Any]) -> Optional[str]:
    """
    从工具调用块中提取 ID

    Args:
        block: 工具调用字典

    Returns:
        工具调用 ID，如果不存在则返回 None
    """
    if isinstance(block.get('id'), str) and block['id']:
        return block['id']

    if isinstance(block.get('call_id'), str) and block['call_id']:
        return block['call_id']

    return None


def extract_tool_calls_from_assistant(msg: AIMessage) -> List[ToolCallLike]:
    """
    从助手消息中提取工具调用列表

    Args:
        msg: 助手消息

    Returns:
        工具调用列表
    """
    tool_calls = getattr(msg, "tool_calls", None)

    if not isinstance(tool_calls, list):
        return []

    calls: List[ToolCallLike] = []

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

def extract_invalid_tool_calls_from_assistant(msg: AIMessage) -> List[ToolCallLike]:
    """
    从助手消息中提取无效工具调用列表

    Args:
        msg: 助手消息

    Returns:
        工具调用列表
    """
    invalid_tool_calls = getattr(msg, "invalid_tool_calls", None)

    if not isinstance(invalid_tool_calls, list):
        return []

    calls: List[ToolCallLike] = []

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


def extract_tool_result_id(msg: ToolMessage) -> Optional[str]:
    """
    从工具结果消息中提取 ID

    Args:
        msg: 工具结果消息

    Returns:
        工具调用 ID，如果不存在则返回 None
    """
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id and isinstance(tool_call_id, str):
        return tool_call_id

    return None


def make_missing_tool_result(tool_call_id: str, tool_name: Optional[str] = None) -> ToolMessage:
    """
    创建缺失的工具结果消息

    Args:
        tool_call_id: 工具调用 ID
        tool_name: 工具名称（可选）

    Returns:
        虚拟的工具结果消息
    """
    return ToolMessage(
        name = tool_name or 'unknown',
        content = "[skill_memory] tool result missing after context trim.",
        tool_call_id = tool_call_id,
        status = "error",
        additional_kwargs = {"timestamp": int(time.time() * 1000)}
    )


def sanitize_tool_use_result_pairing(messages: List[BaseMessage]) -> List[BaseMessage]:
    """
    修复工具调用和结果的配对关系

    裁剪消息后修复 tool_use/toolResult 配对，防止 agent 报 "Message ordering conflict"

    Args:
        messages: 消息列表

    Returns:
        修复后的消息列表
    """
    out: List[BaseMessage] = []
    seen_tool_result_ids: Set[str] = set()
    changed = False

    def push_tool_result(msg: ToolMessage) -> None:
        """添加工具结果消息，避免重复"""
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
            if not (isinstance(msg, ToolMessage) and msg.content):
                out.append(msg)
            else:
                changed = True

            i += 1
            continue

        # 错误状态直接保留
        invalid_tool_calls: List[ToolCallLike] = extract_invalid_tool_calls_from_assistant(msg)

        if getattr(msg, 'status', "") == "error" or len(invalid_tool_calls) > 0:
            out.append(msg)
            i += 1
            continue

        # 提取工具调用
        tool_calls = extract_tool_calls_from_assistant(msg)

        if not tool_calls:
            out.append(msg)
            i += 1
            continue

        tool_call_ids:Set[str] = {t['id'] for t in tool_calls}
        span_results_by_id: Dict[str, ToolMessage] = {}
        remainder: List[ToolMessage] = []

        # 查找后续的工具结果
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

        # 添加助手消息
        out.append(msg)

        if len(span_results_by_id)>0 and len(remainder)>0:
            changed = True

        # 添加工具结果（现有的或虚拟的）
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

        # 添加剩余消息
        for rem in remainder:
            out.append(rem)

        i = j - 1

    return out if changed else messages
