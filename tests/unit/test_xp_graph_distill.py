"""Tests for xp_graph.core.update_draft() — experience trace draft distillation."""

import pytest
from unittest.mock import patch
from langchain_core.messages import HumanMessage, SystemMessage

from context_engine.xp_graph.core import ExperienceTrace, update_draft


class FakeContent:
    """Wrapper for .content attribute on the response."""
    def __init__(self, text: str):
        self.content = text


class FakeChatModel:
    """A fake that mimics langchain BaseChatModel.invoke() returning a JSON string."""

    def __init__(self, json_response: str):
        self._json = json_response

    def bind(self, **kwargs):
        return self

    def invoke(self, input, **kwargs):
        return FakeContent(self._json)


MOCK_JSON = """{
    "task": "test the draft function",
    "path": [
        {
            "tool": "mock_tool",
            "input": "mock input",
            "output": "mock output",
            "trigger": "needed to verify the function works"
        }
    ],
    "failures": [
        {
            "symptom": "mock error",
            "cause": "mock cause",
            "fixes": [
                {
                    "strategy": "parameter",
                    "description": "change a parameter",
                    "tool": null
                }
            ]
        }
    ],
    "requires": ["python"]
}"""


@pytest.fixture
def mock_llm_factory():
    """Patch build_main_llm inside xp_graph.core to return the fake model."""
    with patch("agent.tools.xp_graph.core.build_main_llm") as mock_factory:
        yield mock_factory


@pytest.fixture
def mock_state_register():
    """Patch state_register_mem to avoid side effects and capture set_state calls."""
    with patch("agent.tools.xp_graph.core.state_register_mem") as mock_reg:
        mock_reg.get_state.return_value = ""
        yield mock_reg


# ─── Tests ─────────────────────────────────────────────────────────


class TestUpdateDraft:
    """Tests for the update_draft() function."""

    def test_draft_success(self, mock_llm_factory, mock_state_register):
        """Happy path: LLM returns valid JSON, parser produces ExperienceTrace stored in state."""
        mock_llm_factory.return_value = FakeChatModel(MOCK_JSON)

        update_draft(
            session_id="test-session",
            system_prompt="You are a helpful assistant.",
            messages=[HumanMessage(content="hello")],
        )

        # Verify set_state was called with correct session_id and key
        mock_state_register.set_state.assert_called_once()
        args, _ = mock_state_register.set_state.call_args
        session_id, key, stored = args
        assert session_id == "test-session"
        assert key == "xp_graph_draft"
        assert isinstance(stored, ExperienceTrace)
        assert stored.task == "test the draft function"
        assert len(stored.path) == 1
        assert stored.path[0].tool == "mock_tool"
        assert stored.path[0].trigger == "needed to verify the function works"
        assert len(stored.failures) == 1
        assert stored.failures[0].symptom == "mock error"
        assert len(stored.failures[0].fixes) == 1
        assert stored.failures[0].fixes[0].strategy == "parameter"
        assert stored.requires == ["python"]

    def test_draft_empty_path_and_failures(self, mock_llm_factory, mock_state_register):
        """LLM returns minimal JSON: empty lists, null requires."""
        minimal_json = """{
            "task": "empty test",
            "path": [],
            "failures": [],
            "requires": null
        }"""
        mock_llm_factory.return_value = FakeChatModel(minimal_json)

        update_draft(
            session_id="test-session",
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        args, _ = mock_state_register.set_state.call_args
        stored: ExperienceTrace = args[2]
        assert stored.task == "empty test"
        assert stored.path == []
        assert stored.failures == []
        assert stored.requires is None

    def test_draft_trigger_is_optional(self, mock_llm_factory, mock_state_register):
        """PathStep without 'trigger' field should be parsed as None."""
        json_no_trigger = """{
            "task": "test trigger null",
            "path": [
                {
                    "tool": "web_search",
                    "input": "query",
                    "output": "results"
                }
            ],
            "failures": [],
            "requires": null
        }"""
        mock_llm_factory.return_value = FakeChatModel(json_no_trigger)

        update_draft(
            session_id="test-session",
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        args, _ = mock_state_register.set_state.call_args
        stored: ExperienceTrace = args[2]
        assert len(stored.path) == 1
        assert stored.path[0].tool == "web_search"
        assert stored.path[0].trigger is None

    def test_draft_prompt_contains_trigger_and_required_fields(self):
        """Verify the distill_prompt instructs the LLM about the 'trigger' field."""
        captured_messages = {}

        class CapturingModel(FakeChatModel):
            def invoke(self, input, **kwargs):
                msgs = input.get("messages", [])
                for m in msgs:
                    if isinstance(m, HumanMessage):
                        captured_messages["human"] = m.content
                return FakeContent(self._json)

        mock_json = '{"task":"x","path":[],"failures":[],"requires":null}'
        model = CapturingModel(mock_json)

        with patch("agent.tools.xp_graph.core.build_main_llm", return_value=model), \
             patch("agent.tools.xp_graph.core.state_register_mem") as mock_reg:
            mock_reg.get_state.return_value = ""
            update_draft(
                session_id="test-session",
                system_prompt="system",
                messages=[HumanMessage(content="test msg")],
            )

        human_content = captured_messages.get("human", "")
        assert "trigger" in human_content
        assert "选择此工具/方案的理由" in human_content

    def test_draft_uses_distill_prompt_as_last_message(self, mock_llm_factory, mock_state_register):
        """Ensure the distill_prompt is the final HumanMessage in the LLM call."""
        all_sent_messages = []

        class ObserverModel(FakeChatModel):
            def invoke(self, input, **kwargs):
                nonlocal all_sent_messages
                msgs = input.get("messages", [])
                all_sent_messages = msgs
                return FakeContent(self._json)

        test_messages = [HumanMessage(content="user msg 1"), HumanMessage(content="user msg 2")]
        mock_json = '{"task":"x","path":[],"failures":[],"requires":null}'
        model = ObserverModel(mock_json)

        mock_llm_factory.return_value = model
        update_draft(
            session_id="test-session",
            system_prompt="system prompt text",
            messages=test_messages,
        )

        # Messages: [SystemMessage, *user_msgs, HumanMessage(distill_prompt)]
        assert len(all_sent_messages) == 1 + len(test_messages) + 1  # system + user + prompt
        assert isinstance(all_sent_messages[0], SystemMessage)
        assert all_sent_messages[0].content == "system prompt text"
        # Last message should be the distill_prompt
        assert isinstance(all_sent_messages[-1], HumanMessage)
        assert "ExperienceTrace" in all_sent_messages[-1].content
        assert "task" in all_sent_messages[-1].content
        assert "path" in all_sent_messages[-1].content
        assert "failures" in all_sent_messages[-1].content

    def test_draft_appends_existing_data(self, mock_llm_factory):
        """When existing draft data exists, it's included in the prompt."""
        with patch("agent.tools.xp_graph.core.state_register_mem") as mock_reg:
            # Simulate existing draft data
            mock_reg.get_state.return_value = "Previously extracted data here"

            captured_prompt = {}

            class CapturingModel(FakeChatModel):
                def invoke(self, input, **kwargs):
                    msgs = input.get("messages", [])
                    for m in msgs:
                        if isinstance(m, HumanMessage):
                            captured_prompt["human"] = m.content
                    return FakeContent('{"task":"x","path":[],"failures":[],"requires":null}')

            mock_llm_factory.return_value = CapturingModel('{"task":"x","path":[],"failures":[],"requires":null}')
            update_draft(
                session_id="test-session",
                system_prompt="system",
                messages=[HumanMessage(content="test")],
            )

        human_content = captured_prompt.get("human", "")
        assert "Previously extracted data here" in human_content
        assert "在已有结构化经验上进行改动" in human_content

    def test_draft_does_not_mutate_input_messages(self, mock_llm_factory, mock_state_register):
        """Ensure the original messages list is not mutated by update_draft."""
        original_messages = [HumanMessage(content="msg 1")]
        original_len = len(original_messages)

        mock_llm_factory.return_value = FakeChatModel(MOCK_JSON)
        update_draft(
            session_id="test-session",
            system_prompt="system",
            messages=original_messages,
        )

        # Original messages should be unchanged (no append, no mutation)
        assert len(original_messages) == original_len
        assert original_messages[0].content == "msg 1"
