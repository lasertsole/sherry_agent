"""Plan-Then-Execute plan mode: enter/exit flow, restriction, replan, budget.

The two tools are exercised through their observable contract: the session-state
transitions, the persisted plan file, and what ``exit_plan_mode`` returns for
each approval decision. ``interrupt`` is patched at the module binding (the tool
imports the symbol directly), and the register tiers are replaced by a
dict-backed double so no test touches the real SQLite store.
"""

from __future__ import annotations

import pytest

import agent.tools.plan_mode.exit_plan_mode as exit_mod
from agent.tools.plan_mode.enter_plan_mode import build_enter_plan_mode_tool
from agent.tools.plan_mode.exit_plan_mode import build_exit_plan_mode_tool
from config.features import (
    PLAN_STATUS_IDLE,
    PLAN_STATUS_PLANNING,
    PLAN_STATUS_READY,
    PLAN_STATUS_REPLAN,
)

pytestmark = [pytest.mark.unit]

SESSION = "s-plan-1"


class _FakeRegister:
    """Dict-backed stand-in for a state register tier."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.data[(session_id, str(key))] = value
        return True

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, str(key)), default)


@pytest.fixture()
def registers(monkeypatch):
    """Both tiers replaced by doubles, + a tmp plans dir, + a canned interrupt."""
    mem, db = _FakeRegister(), _FakeRegister()
    import agent.tools.plan_mode.state as state_mod

    monkeypatch.setattr(state_mod, "state_register_mem", mem)
    monkeypatch.setattr(state_mod, "state_register_db", db)
    return {"mem": mem, "db": db, "state_mod": state_mod}


@pytest.fixture()
def plans_dir(tmp_path, monkeypatch):
    directory = tmp_path / SESSION / "plans"
    monkeypatch.setattr("config.path.session_plans_dir", lambda _sid: directory)
    return directory


def _approve(monkeypatch, decision: str, message: str = "") -> dict:
    """Patch the module-bound ``interrupt`` and return the recorded payload."""
    recorded: dict = {}

    def _fake_interrupt(payload):
        recorded["payload"] = payload
        return {"decisions": [{"type": decision, "message": message}]}

    monkeypatch.setattr(exit_mod, "interrupt", _fake_interrupt)
    return recorded


def _status(registers) -> str:
    return str(
        registers["mem"].get_state(SESSION, "plan_status", "")
        or registers["db"].get_state(SESSION, "plan_status", "")
    )


class TestEnterPlanMode:
    def test_entering_sets_planning_and_describes_the_workflow(self, registers):
        tool = build_enter_plan_mode_tool()

        out = tool._run("multi-file refactor", session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_PLANNING
        assert "Plan mode is ON" in out
        assert "four phases" in out
        assert "exit_plan_mode" in out
        assert "may only end with a `question`" in out

    def test_replan_marks_the_status(self, registers):
        tool = build_enter_plan_mode_tool()

        out = tool._run("the approved plan missed the sandbox", replan=True, session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_REPLAN
        assert "re-planning" in out

    def test_both_tiers_receive_the_status(self, registers):
        build_enter_plan_mode_tool()._run("why", session_id=SESSION)

        # The hot-path middleware reads the mem tier; the DB tier keeps the mode
        # across a restart.
        assert registers["mem"].get_state(SESSION, "plan_status") == PLAN_STATUS_PLANNING
        assert registers["db"].get_state(SESSION, "plan_status") == PLAN_STATUS_PLANNING


class TestExitPlanMode:
    def test_submits_persists_and_approves(self, registers, plans_dir, monkeypatch):
        enter = build_enter_plan_mode_tool()
        exit_tool = build_exit_plan_mode_tool()
        enter._run("complex", session_id=SESSION)
        recorded = _approve(monkeypatch, "approve")
        plan = "## Goal\ndo the thing\n\n## Steps\n1. write it\n"

        out = exit_tool._run(plan, session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_READY
        assert "Plan approved" in out
        files = list(plans_dir.glob("plan-*.md"))
        assert len(files) == 1 and files[0].read_text(encoding="utf-8") == plan.strip()
        # The interrupt carried the plan itself for the client to render.
        payload = recorded["payload"]
        action = payload["action_requests"][0]
        assert action["name"] == "exit_plan_mode"
        assert action["args"]["plan"] == plan.strip()
        assert action["args"]["revision"] == files[0].stem.removeprefix("plan-")

    def test_reject_with_feedback_returns_to_planning_and_counts(
        self, registers, plans_dir, monkeypatch
    ):
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        _approve(monkeypatch, "reject", "add a rollback step")
        exit_tool = build_exit_plan_mode_tool()

        out = exit_tool._run("## Goal\nv1\n", session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_PLANNING
        assert "add a rollback step" in out
        assert registers["mem"].get_state(SESSION, "plan_mutation_count") == 1

    def test_reject_without_a_message_drops_the_plan(self, registers, plans_dir, monkeypatch):
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        _approve(monkeypatch, "reject")
        exit_tool = build_exit_plan_mode_tool()

        out = exit_tool._run("## Goal\nv1\n", session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_IDLE
        assert "rejected" in out

    def test_outside_plan_mode_it_is_refused(self, registers):
        exit_tool = build_exit_plan_mode_tool()

        out = exit_tool._run("plan", session_id=SESSION)

        assert "only valid while plan mode is active" in out
        assert _status(registers) == ""

    def test_an_oversized_plan_is_refused_without_persisting(self, registers, plans_dir):
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        exit_tool = build_exit_plan_mode_tool()

        out = exit_tool._run("x" * 20001, session_id=SESSION)

        assert "the limit is 20000" in out
        assert not plans_dir.exists() or not list(plans_dir.glob("plan-*.md"))
        assert _status(registers) == PLAN_STATUS_PLANNING


class TestMutationBudget:
    def test_the_sixth_revision_is_refused(self, registers, plans_dir, monkeypatch):
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        exit_tool = build_exit_plan_mode_tool()
        # Five reject-with-feedback rounds are allowed…
        for i in range(5):
            _approve(monkeypatch, "reject", f"change {i}")
            out = exit_tool._run(f"## Goal\nv{i}\n", session_id=SESSION)
            assert "Revise the plan" in out
        assert registers["mem"].get_state(SESSION, "plan_mutation_count") == 5

        # …the sixth submission is refused: the user must decide.
        _approve(monkeypatch, "reject", "again")
        out = exit_tool._run("## Goal\nv6\n", session_id=SESSION)

        assert "budget" in out and "reject it outright" in out


class TestRevisionBinding:
    def test_a_revision_written_during_the_approval_is_discarded(
        self, registers, plans_dir, monkeypatch
    ):
        from runtime import StateKey

        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        exit_tool = build_exit_plan_mode_tool()
        _approve(monkeypatch, "approve")
        original_interrupt = exit_mod.interrupt

        def _racing_interrupt(payload):
            response = original_interrupt(payload)
            # Something else touches the revision while the approval is pending.
            for register in (registers["mem"], registers["db"]):
                register.set_state(SESSION, str(StateKey.PLAN_REVISION), "deadbeefdeadbeef")
            return response

        monkeypatch.setattr(exit_mod, "interrupt", _racing_interrupt)

        out = exit_tool._run("## Goal\nfresh\n", session_id=SESSION)

        assert "approval was discarded" in out
        assert _status(registers) == PLAN_STATUS_PLANNING

    def test_a_plan_file_edited_during_the_approval_is_discarded(
        self, registers, plans_dir, monkeypatch
    ):
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        exit_tool = build_exit_plan_mode_tool()
        recorded = _approve(monkeypatch, "approve")
        original_interrupt = exit_mod.interrupt

        def _tampering_interrupt(payload):
            response = original_interrupt(payload)
            # The file changes while the human is looking at the approval card.
            for f in plans_dir.glob("plan-*.md"):
                f.write_text("## Goal\nsomeone edited this\n", encoding="utf-8")
            return response

        monkeypatch.setattr(exit_mod, "interrupt", _tampering_interrupt)

        out = exit_tool._run("## Goal\nas submitted\n", session_id=SESSION)

        assert "plan file changed" in out
        assert _status(registers) == PLAN_STATUS_PLANNING
        assert recorded  # the approval did reach the user

    def test_replan_after_approval_is_allowed(self, registers, plans_dir, monkeypatch):
        enter = build_enter_plan_mode_tool()
        exit_tool = build_exit_plan_mode_tool()
        enter._run("complex", session_id=SESSION)
        _approve(monkeypatch, "approve")
        exit_tool._run("## Goal\nv1\n", session_id=SESSION)
        assert _status(registers) == PLAN_STATUS_READY

        enter._run("execution found a flaw", replan=True, session_id=SESSION)

        assert _status(registers) == PLAN_STATUS_REPLAN
        _approve(monkeypatch, "approve")
        out = exit_tool._run("## Goal\nv2\n", session_id=SESSION)
        assert "Plan approved" in out
        assert _status(registers) == PLAN_STATUS_READY
