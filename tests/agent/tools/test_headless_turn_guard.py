"""A turn with no operator must not suspend the graph.

``resolve_turn_operator`` already auto-denies the approval gates for a turn whose
triggering message is a system injection (cron / auto-turn / carrier). The tools
that call ``interrupt()`` THEMSELVES bypass those gates: suspending there leaves
a turn interrupted with nobody able to answer — the definition of a stuck run.
Each one now refuses instead.

The predicate must distinguish "an explicit empty scope" (a headless turn) from
"no scope at all" (a unit test, an auxiliary graph, a direct tool call): the
latter keeps the old suspend-and-wait behaviour.
"""

from __future__ import annotations

import pytest

from agent.middlewares.humanInTheLoop.approval_scope import (
    no_operator_in_scope,
    operator_scope,
)

pytestmark = [pytest.mark.unit]


class TestThePredicate:
    def test_no_scope_is_not_headless(self):
        assert no_operator_in_scope() is False

    def test_an_explicit_operator_is_not_headless(self):
        with operator_scope("session-a"):
            assert no_operator_in_scope() is False

    def test_an_explicit_none_scope_is_headless(self):
        with operator_scope(None):
            assert no_operator_in_scope() is True


class TestQuestionTool:
    def test_a_headless_question_declines_instead_of_suspending(self):
        from agent.tools.question import QuestionTool, QuestionOption

        tool = QuestionTool()
        with operator_scope(None):
            answer = tool._ask(
                question="Which database?",
                header="Database",
                options=[QuestionOption(label="SQLite", description="local")],
                multiple=False,
                session_id="sess-1",
            )

        assert "No human is present" in answer
        assert "Proceed without this information" in answer


class TestExitPlanMode:
    def test_a_headless_submission_is_refused_without_suspending(self):
        from agent.tools.plan_mode.exit_plan_mode import ExitPlanModeTool
        from agent.tools.plan_mode.state import set_plan_state

        session = "sess-headless-plan"
        tool = ExitPlanModeTool()
        set_plan_state(session, status="planning")
        try:
            with operator_scope(None):
                out = tool._submit("## Plan\n1. do the thing", session)
        finally:
            set_plan_state(session, status=None)

        assert out.startswith("Error:")
        assert "no human operator" in out
        assert "Do not retry" in out


class TestSelfPathGate:
    def test_a_headless_self_file_access_fails_closed(self):
        from pathlib import Path

        from agent.tools.pub_base.path_utils import enforce_self_path_policy
        from config.path import ROOT_DIR

        # ``.models`` is one of sherry's own configuration files: the gate
        # classifies it, so the headless branch is the one that decides.
        with operator_scope(None), pytest.raises(Exception) as excinfo:
            enforce_self_path_policy(
                Path(ROOT_DIR) / ".models",
                "sess-1",
                "read the model keys",
                "read",
            )

        assert "automated turn" in str(excinfo.value)
        assert "nobody present" in str(excinfo.value)
