"""Unit tests for extract_final_answer and estimate_msg_tokens.

estimate_msg_tokens is typed `estimate_msg_tokens(msg: BaseMessage)`, but its
`.content` access is duck-typed; we deliberately pass `SimpleNamespace` objects
to exercise that runtime behavior. The file-level pyright suppressions localize
the corresponding type noise to this module.
"""

# pyright: reportUnknownParameterType=false
# pyright: reportArgumentType=false
# pyright: reportAny=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from pub.func.message.extract_final_answer import extract_final_answer
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens


class FakeTextMsg:
    """Minimal stand-in exposing .content and .tool_calls like a LangChain message."""

    content: object
    tool_calls: object | None

    def __init__(self, content: object, tool_calls: object | None = None):
        self.content = content
        self.tool_calls = tool_calls


# --- extract_final_answer ---


import pytest

pytestmark = [pytest.mark.unit]


class TestExtractFinalAnswer:
    def test_returns_last_message_content(self):
        result = {
            "messages": [
                FakeTextMsg("first"),
                FakeTextMsg("final answer"),
            ]
        }
        assert extract_final_answer(result) == "final answer"

    def test_skips_tool_call_messages(self):
        final = FakeTextMsg("the real answer")
        result = {
            "messages": [
                FakeTextMsg("", tool_calls=[{"name": "search"}]),
                final,
            ]
        }
        assert extract_final_answer(result) == "the real answer"

    def test_dict_messages_supported(self):
        result = {
            "messages": [
                {"content": "dict message", "tool_calls": None},
            ]
        }
        assert extract_final_answer(result) == "dict message"

    def test_dict_with_tool_calls_skipped_then_falls_back(self):
        result = {
            "messages": [
                {"content": "", "tool_calls": [{"name": "search"}]},
                {"content": "fallback text", "tool_calls": None},
            ]
        }
        assert extract_final_answer(result) == "fallback text"

    def test_empty_content_skipped(self):
        result = {
            "messages": [
                FakeTextMsg(""),
                FakeTextMsg("valid"),
            ]
        }
        assert extract_final_answer(result) == "valid"

    def test_no_messages_returns_empty(self):
        assert extract_final_answer({}) == ""

    def test_messages_key_missing_returns_empty(self):
        assert extract_final_answer({"other": 1}) == ""


# --- estimate_msg_tokens ---


class TestEstimateMsgTokens:
    def test_string_content(self):
        msg = SimpleNamespace(content="hello world")
        assert estimate_msg_tokens(msg) == len("hello world") // 4

    def test_text_block_list_uses_block_text(self):
        content = [{"type": "text", "text": "hello world"}]
        msg = SimpleNamespace(content=content)
        assert estimate_msg_tokens(msg) == len("hello world") // 4

    def test_empty_string(self):
        msg = SimpleNamespace(content="")
        assert estimate_msg_tokens(msg) == 0

    def test_none_content(self):
        msg = SimpleNamespace(content=None)
        assert estimate_msg_tokens(msg) == 0

    def test_real_langchain_message(self):
        msg = AIMessage(content="reasoned")
        assert estimate_msg_tokens(msg) == len("reasoned") // 4


class TestSystemReminderCarriers:
    """The reminder contract: injected HumanMessages are not user turns.

    A ``<system-reminder>`` (todo list / TaskFlow board / …) is a HumanMessage
    by role — the scanners that walk human messages must skip it, or it becomes
    a phantom turn boundary / a "[User]" line in a summary / an operator-less
    turn for HITL.
    """

    @staticmethod
    def _reminder(source_id: str = "todo_block"):
        from langchain_core.messages import HumanMessage

        from pub.func.message.system_reminder import system_reminder_metadata

        return HumanMessage(
            content=f'<system-reminder source="{source_id}">\n## Todo\n</system-reminder>',
            metadata=system_reminder_metadata(source_id),
        )

    def test_split_into_turns_does_not_break_on_a_reminder(self):
        from langchain_core.messages import AIMessage, HumanMessage

        from pub.func.message.turn_utils import split_into_turns

        messages = [
            HumanMessage(content="first ask"),
            AIMessage(content="answer"),
            self._reminder(),
            HumanMessage(content="second ask"),
            AIMessage(content="answer 2"),
        ]

        turns = split_into_turns(messages)

        assert len(turns) == 2, "the reminder must not open a turn of its own"
        assert turns[0].messages[0].content == "first ask"
        assert turns[1].messages[-1].content == "answer 2"

    def test_the_kind_is_recognised_and_other_carriers_are_not(self):
        from langchain_core.messages import HumanMessage

        from pub.func.message.system_reminder import is_system_reminder

        assert is_system_reminder(self._reminder()) is True
        # The workspace notice keeps its own provenance — not a reminder.
        from pub.func.message.workspace_notice import workspace_notice_metadata

        assert (
            is_system_reminder(
                HumanMessage(content="x", metadata=workspace_notice_metadata("git_head"))
            )
            is False
        )
