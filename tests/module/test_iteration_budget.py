"""Module tests for agent/middlewares/iteration_budget.py — IterationBudget."""

import pytest
from unittest.mock import MagicMock, patch
from runtime.core import Register
from runtime.state_register import StateRegisterMeM


class TestIterationBudget:
    """Test the IterationBudget middleware."""

    @pytest.fixture
    def fresh_state_register(self):
        """Provide a fresh StateRegisterMeM."""
        if StateRegisterMeM in Register._instances:
            del Register._instances[StateRegisterMeM]
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
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            assert ib._consume("s1") is True
            assert ib._consume("s1") is True
            assert ib._consume("s1") is True

    def test_consume_exhausts_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=2)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            assert ib._consume("s1") is True  # used=1
            assert ib._consume("s1") is True  # used=2 (at limit)
            assert ib._consume("s1") is False  # used=2 >= max=2, exhausted

    def test_remaining(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=5)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            assert ib._remaining("s1") == 5
            ib._consume("s1")
            assert ib._remaining("s1") == 4

    def test_before_agent_resets_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=3)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            # Consume some budget
            ib._consume("s1")
            ib._consume("s1")
            # Reset via before_agent
            state = self._make_state("s1")
            ib._before_agent_impl(state)
            assert ib._remaining("s1") == 3

    def test_wrap_model_call_within_budget(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=5)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            request = MagicMock()
            request.state = self._make_state("s1")
            handler = MagicMock(return_value=MagicMock(name="ModelResponse"))
            result = ib._wrap_model_call_impl(request)
            assert result is None  # None means proceed to handler

    def test_wrap_model_call_exhausted_returns_terminal(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=1)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            request = MagicMock()
            request.state = self._make_state("s1")
            # Consume budget
            ib._consume("s1")
            result = ib._wrap_model_call_impl(request)
            assert result is not None
            assert "exhausted" in result.content.lower() or "budget" in result.content.lower()

    def test_wrap_tool_call_exhausted_returns_terminal(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=1)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            request = MagicMock()
            request.state = self._make_state("s1")
            request.tool_call = {"name": "test_tool", "id": "call_123"}
            # Consume budget
            ib._consume("s1")
            result = ib._wrap_tool_call_impl(request)
            assert result is not None
            assert "budget" in result.content.lower() or "exhausted" in result.content.lower()

    def test_separate_sessions_independent(self, fresh_state_register):
        ib = self._make_middleware(max_iterations=1)
        with patch("agent.middlewares.iteration_budget.state_register_mem", fresh_state_register):
            assert ib._consume("s1") is True  # s1 used=1
            assert ib._consume("s2") is True  # s2 used=1 (fresh)
            assert ib._consume("s1") is False  # s1 exhausted
            assert ib._consume("s2") is False  # s2 exhausted

    def test_key_constants(self):
        from agent.middlewares.iteration_budget import IterationBudget
        assert IterationBudget._BUDGET_KEY == "iteration_budget"
        assert IterationBudget._USED_KEY == "iteration_budget_used"
