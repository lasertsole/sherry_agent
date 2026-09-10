"""Unit tests for agent/tools/question.py — schema bounds, interrupt payload, decisions."""

from typing import Any

import pytest
from pydantic import ValidationError

import agent.tools.question as question_module
from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.question import QuestionInput, build_question_tool


pytestmark = [pytest.mark.unit]


def _options(n: int) -> list[dict[str, str]]:
    return [{"label": f"option {i}", "description": f"description {i}"} for i in range(1, n + 1)]


def _patch_interrupt(monkeypatch, response: dict[str, Any]):
    captured: dict[str, Any] = {}

    def _fake_interrupt(value: Any) -> dict[str, Any]:
        captured["value"] = value
        return response

    monkeypatch.setattr(question_module, "interrupt", _fake_interrupt)
    return captured


# ============================================================================
# QuestionInput schema bounds
# ============================================================================


class TestQuestionInputSchema:
    def test_valid_input(self):
        s = QuestionInput(
            question="Which database?",
            header="DB choice",
            options=_options(2),
        )
        assert s.header == "DB choice"
        assert s.multiple is False

    def test_header_max_30_chars(self):
        QuestionInput(question="q", header="a" * 30, options=_options(2))
        with pytest.raises(ValidationError):
            QuestionInput(question="q", header="a" * 31, options=_options(2))

    def test_options_minimum_two(self):
        with pytest.raises(ValidationError):
            QuestionInput(question="q", header="h", options=_options(1))

    def test_options_maximum_six(self):
        QuestionInput(question="q", header="h", options=_options(6))
        with pytest.raises(ValidationError):
            QuestionInput(question="q", header="h", options=_options(7))


# ============================================================================
# Interrupt payload shape (HITL contract)
# ============================================================================


class TestInterruptPayload:
    def test_interrupt_receives_hitl_request_shape(self, monkeypatch):
        captured = _patch_interrupt(
            monkeypatch, {"decisions": [{"type": "approve", "message": "Postgres"}]}
        )
        tool = build_question_tool()

        tool.invoke(
            {
                "question": "Which database?",
                "header": "DB choice",
                "options": _options(2),
                "multiple": False,
            }
        )

        value = captured["value"]
        assert value["action_requests"][0]["name"] == "question"
        assert value["action_requests"][0]["description"] == "Which database?"
        args = value["action_requests"][0]["args"]
        assert args == {
            "question": "Which database?",
            "header": "DB choice",
            "options": [
                {"label": "option 1", "description": "description 1"},
                {"label": "option 2", "description": "description 2"},
            ],
            "multiple": False,
        }
        assert value["review_configs"] == [
            {"action_name": "question", "allowed_decisions": ["approve", "reject"]}
        ]


# ============================================================================
# Decision handling
# ============================================================================


class TestDecisionHandling:
    def _invoke(self, monkeypatch, response: dict[str, Any]) -> str:
        _patch_interrupt(monkeypatch, response)
        tool = build_question_tool()
        return tool.invoke(
            {"question": "Which database?", "header": "DB choice", "options": _options(2)}
        )

    def test_approve_returns_user_message(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": [{"type": "approve", "message": "Postgres"}]})
        assert 'User answered: "Postgres". You can now continue.' == out

    def test_approve_without_message_returns_empty_answer(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": [{"type": "approve"}]})
        assert 'User answered: ""' in out

    def test_reject_returns_decline(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": [{"type": "reject"}]})
        assert out == "User declined to answer. Proceed without this information."

    def test_empty_decisions_return_decline(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": []})
        assert out == "User declined to answer. Proceed without this information."


# ============================================================================
# Factory wiring
# ============================================================================


class TestFactoryWiring:
    def test_build_question_tool_config(self):
        tool = build_question_tool()
        assert tool.name == "question"
        assert tool.handle_tool_error is True
        assert tool.metadata == {"idempotent": True, "skip_heartbeat": True, "nudge": True}
        assert tool.args_schema is QuestionInput

    def test_registered_in_main_tools_builders(self):
        from agent.tools import build_question_tool

        assert build_question_tool in _MAIN_TOOLS_BUILDERS
