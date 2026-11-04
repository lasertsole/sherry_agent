"""Unit tests for agent/tools/question.py — schema bounds, interrupt payload, decisions.

Covers both modes: the historical flat single question and the batch
``questions`` array (1-4 questions, per-question annotation / preview), plus the
JSON answer formatting the model receives after a batch resume.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

import agent.tools.question as question_module
from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.question import (
    QuestionInput,
    QuestionOption,
    QuestionSpec,
    _format_answer,
    build_question_tool,
)


pytestmark = [pytest.mark.unit]


def _options(n: int) -> list[dict[str, str]]:
    return [{"label": f"option {i}", "description": f"description {i}"} for i in range(1, n + 1)]


def _spec(question: str = "Which database?", header: str = "DB") -> QuestionSpec:
    return QuestionSpec(question=question, header=header, options=_options(2))


def _patch_interrupt(monkeypatch, response: dict[str, Any]):
    captured: dict[str, Any] = {}

    def _fake_interrupt(value: Any) -> dict[str, Any]:
        captured["value"] = value
        return response

    monkeypatch.setattr(question_module, "interrupt", _fake_interrupt)
    return captured


# ============================================================================
# Schema
# ============================================================================


class TestQuestionInputSchema:
    def test_valid_flat_input(self):
        s = QuestionInput(
            question="Which database?",
            header="DB choice",
            options=_options(2),
        )
        assert s.header == "DB choice"
        assert s.multiple is False
        assert s.questions == []

    def test_header_max_30_chars(self):
        QuestionInput(question="q", header="a" * 30, options=_options(2))
        with pytest.raises(ValidationError):
            QuestionInput(question="q", header="a" * 31, options=_options(2))
        with pytest.raises(ValidationError):
            QuestionSpec(question="q", header="a" * 31, options=_options(2))

    def test_flat_options_maximum_six(self):
        QuestionInput(question="q", header="h", options=_options(6))
        with pytest.raises(ValidationError):
            QuestionInput(question="q", header="h", options=_options(7))

    def test_flat_options_may_be_empty_for_the_batch_mode(self):
        # The batch shape leaves the deprecated flat fields empty; `_ask` then
        # validates the array. A flat call with <2 options is refused by `_ask`.
        assert QuestionInput(questions=[_spec().model_dump()], options=[]).questions
        tool = build_question_tool()
        assert tool._ask(question="q", header="h", options=[], multiple=False) == (
            "Error: question needs at least 2 options."
        )

    def test_batch_accepts_one_to_four_questions(self):
        one = QuestionInput(questions=[_spec().model_dump()])
        assert len(one.questions) == 1
        four = QuestionInput(questions=[_spec().model_dump() for _ in range(4)])
        assert len(four.questions) == 4
        with pytest.raises(ValidationError):
            QuestionInput(questions=[_spec().model_dump() for _ in range(5)])

    def test_each_question_requires_two_to_six_options(self):
        with pytest.raises(ValidationError):
            QuestionSpec(question="q", header="h", options=_options(1))
        QuestionSpec(question="q", header="h", options=_options(6))
        with pytest.raises(ValidationError):
            QuestionSpec(question="q", header="h", options=_options(7))

    def test_question_defaults(self):
        spec = _spec()
        assert spec.multiple is False
        assert spec.allow_annotation is True

    def test_option_preview_defaults_to_empty(self):
        assert QuestionOption(label="a", description="d").preview == ""
        assert QuestionOption(label="a", description="d", preview="<x/>").preview == "<x/>"


# ============================================================================
# Interrupt payload shape (HITL contract)
# ============================================================================


class TestInterruptPayload:
    def test_flat_call_keeps_the_legacy_shape(self, monkeypatch):
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
        # The flat fields stay for an older frontend…
        assert args["question"] == "Which database?"
        assert args["header"] == "DB choice"
        assert args["options"] == [
            {"label": "option 1", "description": "description 1", "preview": ""},
            {"label": "option 2", "description": "description 2", "preview": ""},
        ]
        assert args["multiple"] is False
        # …and the new batch fields are always present.
        assert args["_multi"] is False
        assert len(args["questions"]) == 1
        assert args["questions"][0]["header"] == "DB choice"
        assert value["review_configs"] == [
            {"action_name": "question", "allowed_decisions": ["approve", "reject"]}
        ]

    def test_batch_call_carries_every_question_and_the_multi_flag(self, monkeypatch):
        captured = _patch_interrupt(monkeypatch, {"decisions": [{"type": "reject"}]})
        tool = build_question_tool()

        tool.invoke(
            {
                "questions": [
                    {
                        "question": "Which framework?",
                        "header": "Framework",
                        "options": [
                            {"label": "Vue", "description": "Vue 3", "preview": "<App/>"},
                            {"label": "React", "description": "React 19"},
                        ],
                        "multiple": False,
                    },
                    {
                        "question": "Add SSR?",
                        "header": "SSR",
                        "options": [
                            {"label": "Yes", "description": "Nitro"},
                            {"label": "No", "description": "SPA"},
                        ],
                        "multiple": True,
                        "allow_annotation": False,
                    },
                ]
            }
        )

        args = captured["value"]["action_requests"][0]["args"]
        assert args["_multi"] is True
        assert [q["header"] for q in args["questions"]] == ["Framework", "SSR"]
        assert args["questions"][0]["options"][0]["preview"] == "<App/>"
        assert args["questions"][1]["multiple"] is True
        assert args["questions"][1]["allow_annotation"] is False
        # The flat fields describe the FIRST question, for older renderers.
        assert args["question"] == "Which framework?"
        assert args["header"] == "Framework"


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

    def test_yolo_treated_as_approval(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": [{"type": "yolo", "message": "Chinese"}]})
        assert out == 'User answered: "Chinese". You can now continue.'

    def test_yolo_activates_session_yolo(self, monkeypatch):
        import agent.middlewares.humanInTheLoop.approval as approval_module

        calls: list[str] = []
        monkeypatch.setattr(approval_module, "set_session_yolo", lambda sid: calls.append(sid))
        _patch_interrupt(monkeypatch, {"decisions": [{"type": "yolo", "message": "Chinese"}]})
        tool = build_question_tool()
        tool._ask(
            "Which database?",
            "DB choice",
            [
                QuestionOption(label="a", description="a"),
                QuestionOption(label="b", description="b"),
            ],
            False,
            session_id="sess-1",
        )
        assert calls == ["sess-1"]

    def test_reject_returns_decline(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": [{"type": "reject"}]})
        assert out == "User declined to answer. Proceed without this information."

    def test_empty_decisions_return_decline(self, monkeypatch):
        out = self._invoke(monkeypatch, {"decisions": []})
        assert out == "User declined to answer. Proceed without this information."

    def test_a_batch_resume_formats_every_answer(self, monkeypatch):
        answers = {
            "answers": [
                {"question": "Which framework?", "header": "Framework", "selection": ["Vue"]},
                {
                    "question": "Add SSR?",
                    "header": "SSR",
                    "selection": ["Yes"],
                    "annotation": "use Nitro",
                },
            ]
        }
        _patch_interrupt(
            monkeypatch, {"decisions": [{"type": "approve", "message": json.dumps(answers)}]}
        )
        out = build_question_tool().invoke(
            {
                "questions": [
                    {
                        "question": "Which framework?",
                        "header": "Framework",
                        "options": _options(2),
                    },
                    {"question": "Add SSR?", "header": "SSR", "options": _options(2)},
                ]
            }
        )

        assert out == (
            "User answered:\n"
            "• Framework → Vue\n"
            "• SSR → Yes (note: use Nitro)\n"
            "You can now continue."
        )


# ============================================================================
# Answer formatting (the batch JSON contract)
# ============================================================================


class TestFormatAnswer:
    QUESTIONS = [
        QuestionSpec(question="Which framework?", header="Framework", options=_options(2)),
        QuestionSpec(question="Add SSR?", header="SSR", options=_options(2)),
    ]

    def test_single_mode_keeps_the_plain_answer(self):
        assert _format_answer("Vue", [self.QUESTIONS[0]], False) == (
            'User answered: "Vue". You can now continue.'
        )

    def test_batch_json_becomes_one_line_per_question(self):
        message = json.dumps(
            {
                "answers": [
                    {"question": "Which framework?", "header": "Framework", "selection": ["Vue"]},
                    {"question": "Add SSR?", "header": "SSR", "selection": ["Yes"]},
                ]
            }
        )
        assert _format_answer(message, self.QUESTIONS, True) == (
            "User answered:\n• Framework → Vue\n• SSR → Yes\nYou can now continue."
        )

    def test_annotation_is_appended(self):
        message = json.dumps(
            {
                "answers": [
                    {"header": "Framework", "selection": ["Vue"], "annotation": "needs Nitro"},
                    {"header": "SSR", "selection": ["Yes"], "annotation": "  "},
                ]
            }
        )
        out = _format_answer(message, self.QUESTIONS, True)
        assert "• Framework → Vue (note: needs Nitro)" in out
        assert "• SSR → Yes\n" in out, "an empty/blank annotation must not append a note"

    def test_empty_selection_reads_as_no_selection(self):
        message = json.dumps({"answers": [{"header": "Framework", "selection": []}]})
        out = _format_answer(message, self.QUESTIONS, True)
        assert "• Framework → (no selection)" in out
        assert "• SSR → (no selection)" in out, "a missing answer still gets its line"

    def test_a_string_selection_is_accepted(self):
        message = json.dumps({"answers": [{"header": "Framework", "selection": "Vue"}]})
        assert "• Framework → Vue" in _format_answer(message, self.QUESTIONS, True)

    def test_malformed_json_falls_back_to_the_plain_answer(self):
        assert _format_answer("not json", self.QUESTIONS, True) == (
            'User answered: "not json". You can now continue.'
        )

    def test_a_non_list_answers_payload_falls_back(self):
        message = json.dumps({"answers": "oops"})
        out = _format_answer(message, self.QUESTIONS, True)
        assert out.startswith('User answered: "'), out

    def test_the_header_falls_back_to_the_question_text(self):
        specs = [QuestionSpec(question="Which framework?", header="", options=_options(2))]
        message = json.dumps({"answers": [{"selection": ["Vue"]}]})
        assert "• Which framework? → Vue" in _format_answer(message, specs, True)


# ============================================================================
# Headless turns
# ============================================================================


class TestHeadlessTurns:
    def _deny(self, monkeypatch):
        import agent.middlewares.humanInTheLoop.approval_scope as scope_module

        monkeypatch.setattr(scope_module, "no_operator_in_scope", lambda: True)

    def test_a_batch_headless_turn_declines_without_suspending(self, monkeypatch):
        self._deny(monkeypatch)
        monkeypatch.setattr(
            question_module, "interrupt", lambda _value: pytest.fail("must not suspend")
        )
        out = build_question_tool().invoke(
            {
                "questions": [
                    {"question": "q1", "header": "H1", "options": _options(2)},
                    {"question": "q2", "header": "H2", "options": _options(2)},
                ]
            }
        )
        assert out == question_module._HEADLESS_DECLINE_MESSAGE

    def test_a_flat_headless_turn_declines_too(self, monkeypatch):
        self._deny(monkeypatch)
        monkeypatch.setattr(
            question_module, "interrupt", lambda _value: pytest.fail("must not suspend")
        )
        out = build_question_tool().invoke({"question": "q", "header": "h", "options": _options(2)})
        assert out == question_module._HEADLESS_DECLINE_MESSAGE


# ============================================================================
# Factory wiring
# ============================================================================


class TestFactoryWiring:
    def test_build_question_tool_config(self):
        tool = build_question_tool()
        assert tool.name == "question"
        assert tool.handle_tool_error is True
        assert tool.metadata == {
            "idempotent": True,
            "skip_heartbeat": True,
            "nudge": True,
            "side_effect_scope": "user_interaction",
        }
        assert tool.args_schema is QuestionInput

    def test_registered_in_main_tools_builders(self):
        from agent.tools import build_question_tool as builder

        assert builder in _MAIN_TOOLS_BUILDERS
