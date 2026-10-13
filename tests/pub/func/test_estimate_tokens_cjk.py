"""Integration: CJK message lists must no longer be underestimated.

End-to-end over ``estimate_messages_tokens``: a CJK conversation estimates
strictly above the legacy ``len // CHARS_PER_TOKEN`` formula (the bug this
change fixes), while an ASCII conversation keeps the legacy numbers exactly
(the regression floor).

The legacy estimator is reimplemented here as the pre-change baseline so the
comparison is independent of the code under test.
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.features import TOKEN_ESTIMATION
from pub.func.estimate_tokens import estimate_messages_tokens

pytestmark = [pytest.mark.integration]

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]


def _legacy_estimate_msg(msg) -> int:
    """The pre-change per-message estimate: total chars // CHARS_PER_TOKEN."""
    total = 0
    content = msg.content
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        total += len(json.dumps(content))
    for tc in getattr(msg, "tool_calls", None) or []:
        total += len(str(tc.get("name", "")))
        total += len(str(tc.get("args", "")))
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        total += len(str(tool_call_id))
    return total // CHARS_PER_TOKEN


def _legacy_estimate(messages) -> int:
    return sum(_legacy_estimate_msg(msg) for msg in messages)


def test_cjk_message_list_estimates_above_legacy():
    messages = [
        HumanMessage(content="你好，请帮我分析这个问题的原因。"),
        AIMessage(content="好的，我来逐步分析这个问题。"),
        ToolMessage(content="分析结果：共有三个主要原因。", tool_call_id="call_1"),
    ]

    assert estimate_messages_tokens(messages) > _legacy_estimate(messages)


def test_ascii_message_list_keeps_legacy_numbers():
    messages = [
        HumanMessage(content="x" * 4000),
        AIMessage(content="y" * 800),
        ToolMessage(content="z" * 1600, tool_call_id="call_1"),
    ]

    assert estimate_messages_tokens(messages) == _legacy_estimate(messages)


def test_cjk_tool_message_estimates_above_legacy():
    msg = ToolMessage(content="分析结果：共有三个主要原因。", tool_call_id="call_1")

    assert estimate_messages_tokens([msg]) > _legacy_estimate([msg])
