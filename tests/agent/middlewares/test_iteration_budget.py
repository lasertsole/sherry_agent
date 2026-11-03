"""Module tests for agent/middlewares/iteration_budget/core.py — IterationBudget."""

import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from runtime.session.core import SessionRegister
from runtime.session.state_register import StateRegisterMeM


pytestmark = [pytest.mark.module]


class TestIterationBudget:
    """Test the IterationBudget middleware."""

    @pytest.fixture
    def fresh_state_register(self, monkeypatch):
        """Provide a fresh StateRegisterMeM."""
        monkeypatch.delitem(SessionRegister._instances, StateRegisterMeM, raising=False)
        reg = StateRegisterMeM()
        yield reg

    def _make_middleware(self, max_iterations=None):
        """Create an IterationBudget with a fresh state register."""
        from agent.middlewares.iteration_budget import IterationBudget

        if max_iterations is None:
            return IterationBudget()
        return IterationBudget(max_iterations=max_iterations)

    def _make_state(self, session_id="test-session"):
        """Create a mock AgentState dict."""
        return {"session_id": session_id}

    def test_init_default_max(self):
        ib = self._make_middleware()
        assert ib.max_iterations == 50

    def test_init_custom_max(self):
        ib = self._make_middleware(max_iterations=10)
        assert ib.max_iterations == 10

    def test_get_session_id(self):
        ib = self._make_middleware()
        state = self._make_state("s1")
        assert ib._get_session_id(state) == "s1"

    def test_get_session_id_empty_raises(self):
        ib = self._make_middleware()
        state = {"session_id": "  "}
        with pytest.raises(RuntimeError, match="session_id is required"):
            ib._get_session_id(state)

    def test_consume_within_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=3)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            assert ib._consume("s1") is True
            assert ib._consume("s1") is True
            assert ib._consume("s1") is True

    def test_consume_exhausts_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=2)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            assert ib._consume("s1") is True  # used=1
            assert ib._consume("s1") is True  # used=2 (at limit)
            assert ib._consume("s1") is False  # used=2 >= max=2, exhausted

    def test_remaining(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=5)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            assert ib._remaining("s1") == 5
            ib._consume("s1")
            assert ib._remaining("s1") == 4

    def test_before_agent_resets_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=3)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            # Consume some budget
            ib._consume("s1")
            ib._consume("s1")
            # Reset via before_agent
            state = self._make_state("s1")
            ib._before_agent_impl(state)
            assert ib._remaining("s1") == 3

    def test_wrap_model_call_within_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=5)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            request = MagicMock()
            request.state = self._make_state("s1")
            result = ib._wrap_model_call_impl(request)
            assert result is None  # None means proceed to handler

    def test_wrap_model_call_exhausted_returns_terminal(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=1)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            request = MagicMock()
            request.state = self._make_state("s1")
            # Consume budget
            ib._consume("s1")
            result = ib._wrap_model_call_impl(request)
            assert result is not None
            assert "exhausted" in result.content.lower() or "budget" in result.content.lower()

    def test_budget_warning_injected_once_at_the_ratio(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=5)  # warn at int(5 * 0.8) = 4
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            request = MagicMock()
            request.state = self._make_state("s1")
            request.messages = [HumanMessage(content="hello")]
            prepared = MagicMock(name="prepared")
            request.override.return_value = prepared

            results = [ib._wrap_model_call_impl(request) for _ in range(5)]

            assert results[:3] == [None, None, None]
            assert results[3] is prepared
            assert results[4] is None  # latch: injected once per turn
            request.override.assert_called_once()
            injected = request.override.call_args.kwargs["messages"][-1]
            assert "[SYSTEM DIRECTIVE: ITERATION BUDGET]" in injected.content
            assert injected.metadata["provenance"] == "iteration_budget"

            # The latch resets with the per-turn counters.
            ib._before_agent_impl(self._make_state("s1"))
            assert ib._wrap_model_call_impl(request) is None  # used=1

    def test_wrap_model_call_forwards_the_prepared_request(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=2)  # warn at 1
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            request = MagicMock()
            request.state = self._make_state("s1")
            request.messages = [HumanMessage(content="hello")]
            prepared = MagicMock(name="prepared")
            request.override.return_value = prepared
            handler = MagicMock(return_value="response")

            assert ib.wrap_model_call(request, handler) == "response"
            handler.assert_called_once_with(prepared)

    def test_tool_calls_make_no_budget_hook(self):
        """A tool-call pair costs ONE unit: only the AIMessage-producing model call."""
        from langchain.agents.middleware import AgentMiddleware

        from agent.middlewares.iteration_budget import IterationBudget

        assert IterationBudget.wrap_tool_call is AgentMiddleware.wrap_tool_call
        assert IterationBudget.awrap_tool_call is AgentMiddleware.awrap_tool_call

    def test_model_calls_are_the_only_consumers(self, fresh_state_register):
        """Tool results, HumanMessages and SystemMessages never charge the counter."""
        ib = self._make_middleware(max_iterations=2)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            state = self._make_state("s1")
            # A pending user message / system reminder in state costs nothing on
            # its own; only the model call that follows it does.
            state["messages"] = [HumanMessage(content="hello")]
            assert ib._remaining("s1") == 2

            request = MagicMock()
            request.state = state
            # 80% of a 2-call budget is reached on the first call; the harness
            # only cares that the counter charges, so keep the model view as-is.
            request.override.return_value = None
            assert ib._wrap_model_call_impl(request) is None  # model call 1
            assert ib._remaining("s1") == 1

            # The ToolMessage result is free; the next model call pays.
            state["messages"] = [ToolMessage(content="tool result", tool_call_id="c1")]
            assert ib._remaining("s1") == 1
            assert ib._wrap_model_call_impl(request) is None  # model call 2
            assert ib._remaining("s1") == 0
            terminal = ib._wrap_model_call_impl(request)  # model call 3 refused
            assert isinstance(terminal, AIMessage)

    def test_separate_sessions_independent(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=1)
        with patch(
            "agent.middlewares.iteration_budget.core.state_register_mem", fresh_state_register
        ):
            assert ib._consume("s1") is True  # s1 used=1
            assert ib._consume("s2") is True  # s2 used=1 (fresh)
            assert ib._consume("s1") is False  # s1 exhausted
            assert ib._consume("s2") is False  # s2 exhausted

    def test_key_constants(self):
        from agent.middlewares.iteration_budget import IterationBudget

        assert IterationBudget._BUDGET_KEY == "iteration_budget"
        assert IterationBudget._USED_KEY == "iteration_budget_used"
