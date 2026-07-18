"""Tests for xp_graph.core.distill_trace() — experience trace distillation via LLM."""

import pytest
from unittest.mock import AsyncMock, patch
from langchain_core.messages import HumanMessage, SystemMessage

from context_engine.xp_graph.core import ExperienceTrace, PathStep, Failure, Fix, distill_trace


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
    "task": "test the distill function",
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

MOCK_MINIMAL_JSON = """{
    "task": "empty test",
    "path": [],
    "failures": [],
    "requires": null
}"""


@pytest.fixture
def mock_llm_factory():
    """Patch build_main_llm inside xp_graph.core to return the fake model."""
    with patch("context_engine.xp_graph.core.build_main_llm") as mock_factory:
        yield mock_factory


class TestDistillTrace:
    """Tests for the distill_trace() function."""

    @pytest.mark.asyncio
    async def test_distill_success(self, mock_llm_factory):
        """Happy path: LLM returns valid JSON, parser produces ExperienceTrace."""
        mock_llm_factory.return_value = FakeChatModel(MOCK_JSON)

        result = await distill_trace(
            system_prompt="You are a helpful assistant.",
            messages=[HumanMessage(content="hello")],
        )

        assert isinstance(result, ExperienceTrace)
        assert result.task == "test the distill function"
        assert len(result.path) == 1
        assert result.path[0].tool == "mock_tool"
        assert result.path[0].trigger == "needed to verify the function works"
        assert len(result.failures) == 1
        assert result.failures[0].symptom == "mock error"
        assert len(result.failures[0].fixes) == 1
        assert result.failures[0].fixes[0].strategy == "parameter"
        assert result.requires == ["python"]

    @pytest.mark.asyncio
    async def test_distill_empty_path_and_failures(self, mock_llm_factory):
        """LLM returns minimal JSON: empty lists, null requires."""
        mock_llm_factory.return_value = FakeChatModel(MOCK_MINIMAL_JSON)

        result = await distill_trace(
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        assert result.task == "empty test"
        assert result.path == []
        assert result.failures == []
        assert result.requires is None

    @pytest.mark.asyncio
    async def test_distill_trigger_is_optional(self, mock_llm_factory):
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

        result = await distill_trace(
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        assert len(result.path) == 1
        assert result.path[0].tool == "web_search"
        assert result.path[0].trigger is None

    @pytest.mark.asyncio
    async def test_distill_prompt_contains_trigger_and_required_fields(self):
        """Verify the distill_prompt instructs the LLM about the 'trigger' field."""
        captured_messages = {}

        class CapturingModel(FakeChatModel):
            def invoke(self, input, **kwargs):
                msgs = input if isinstance(input, list) else input.get("messages", [])
                for m in msgs:
                    if isinstance(m, HumanMessage):
                        # Track the last HumanMessage (distill prompt)
                        captured_messages["human"] = m.content
                return FakeContent(self._json)

        model = CapturingModel(MOCK_MINIMAL_JSON)

        with patch("context_engine.xp_graph.core.build_main_llm", return_value=model):
            await distill_trace(
                system_prompt="system",
                messages=[HumanMessage(content="test msg")],
            )

        human_content = captured_messages.get("human", "")
        assert "trigger" in human_content
        assert "选择此工具/方案的理由" not in human_content  # old prompt text

    @pytest.mark.asyncio
    async def test_distill_uses_distill_prompt_as_last_message(self, mock_llm_factory):
        """Ensure the distill_prompt is the final HumanMessage in the LLM call."""
        all_sent_messages = []

        class ObserverModel(FakeChatModel):
            def invoke(self, input, **kwargs):
                nonlocal all_sent_messages
                all_sent_messages = input if isinstance(input, list) else input.get("messages", [])
                return FakeContent(self._json)

        test_messages = [HumanMessage(content="user msg 1"), HumanMessage(content="user msg 2")]
        model = ObserverModel(MOCK_MINIMAL_JSON)

        mock_llm_factory.return_value = model
        await distill_trace(
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

    @pytest.mark.asyncio
    async def test_distill_does_not_mutate_input_messages(self, mock_llm_factory):
        """Ensure the original messages list is not mutated by _distill_trace."""
        original_messages = [HumanMessage(content="msg 1")]
        original_len = len(original_messages)

        mock_llm_factory.return_value = FakeChatModel(MOCK_JSON)
        await distill_trace(
            system_prompt="system",
            messages=original_messages,
        )

        # Original messages should be unchanged (no append, no mutation)
        assert len(original_messages) == original_len
        assert original_messages[0].content == "msg 1"

    @pytest.mark.asyncio
    async def test_distill_appends_distill_prompt_without_previous_draft(self, mock_llm_factory):
        """When there's no existing draft, the distill prompt simply follows the user messages.

        This differs from the old update_draft which appended existing draft data.
        _distill_trace does NOT look up previous draft data — it just distills what's given.
        """
        all_sent_messages = []

        class ObserverModel(FakeChatModel):
            def invoke(self, input, **kwargs):
                nonlocal all_sent_messages
                all_sent_messages = input if isinstance(input, list) else input.get("messages", [])
                return FakeContent(self._json)

        mock_llm_factory.return_value = ObserverModel(MOCK_MINIMAL_JSON)
        await distill_trace(
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        # Messages: [SystemMessage, HumanMessage(user), HumanMessage(distill_prompt)]
        assert len(all_sent_messages) == 3
        assert isinstance(all_sent_messages[-1], HumanMessage)
        # No previous draft data in the prompt
        assert "在已有结构化经验上进行改动" not in all_sent_messages[-1].content

    @pytest.mark.asyncio
    async def test_distill_uses_json_mode(self, mock_llm_factory):
        """Verify the LLM is called with response_format={'type': 'json_object'}."""
        class BindingObserver(FakeChatModel):
            def __init__(self, json_response: str):
                super().__init__(json_response)
                self._bound_kwargs = None

            def bind(self, **kwargs):
                self._bound_kwargs = kwargs
                return self

        model = BindingObserver(MOCK_JSON)
        mock_llm_factory.return_value = model

        await distill_trace(
            system_prompt="system",
            messages=[HumanMessage(content="test")],
        )

        assert model._bound_kwargs is not None
        assert model._bound_kwargs.get("response_format") == {"type": "json_object"}
