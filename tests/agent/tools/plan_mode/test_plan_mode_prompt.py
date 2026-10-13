"""Plan-Then-Execute: the prompt injection and the plan file's life on disk.

The prompt block is rebuilt from live state on every call (so entering/leaving
plan mode takes effect on the next model call and a compression cannot lose it);
the plan file is session-scoped and named after the plan's revision, so the
re-executed call that follows a resume writes the same file.
"""

from __future__ import annotations

import pytest

from agent.tools.plan_mode.enter_plan_mode import build_enter_plan_mode_tool
from agent.tools.plan_mode.exit_plan_mode import build_exit_plan_mode_tool
from runtime.session.state_keys import StateKey
from workspace.prompt_builder import _build_plan_mode_block

pytestmark = [pytest.mark.unit]

SESSION = "sess-plan-prompt"


class _FakeRegister:
    """Dict-backed stand-in: no test may touch the real state stores."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.data[(session_id, str(key))] = value
        return True

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, str(key)), default)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    fake_mem, fake_db = _FakeRegister(), _FakeRegister()
    import agent.tools.plan_mode.state as plan_state

    monkeypatch.setattr(plan_state, "state_register_mem", fake_mem)
    monkeypatch.setattr(plan_state, "state_register_db", fake_db)
    # The prompt block imports the tiers from ``runtime`` directly.
    monkeypatch.setattr("runtime.state_register_mem", fake_mem)
    monkeypatch.setattr("runtime.state_register_db", fake_db)
    return {"mem": fake_mem, "db": fake_db}


class TestPlanModePromptBlock:
    def test_idle_sessions_get_no_block(self):
        assert _build_plan_mode_block(SESSION) == ""

    def test_planning_carries_the_workflow_and_the_end_rule(self):
        build_enter_plan_mode_tool()._run("multi-file change", session_id=SESSION)

        block = _build_plan_mode_block(SESSION)

        assert "Plan Mode (Plan-Then-Execute)" in block
        assert "four phases" in block
        assert "exit_plan_mode" in block
        assert "may ONLY end with a `question`" in block
        assert "read_file" in block and "write_file" in block

    def test_replan_is_labelled(self):
        build_enter_plan_mode_tool()._run("plan missed a case", replan=True, session_id=SESSION)

        block = _build_plan_mode_block(SESSION)

        assert "RE-PLAN" in block
        assert "no longer fits execution" in block

    def test_an_approved_plan_is_referenced_by_path(self, _isolated_state):
        _isolated_state["mem"].set_state(SESSION, str(StateKey.PLAN_STATUS), "ready")
        _isolated_state["mem"].set_state(
            SESSION, str(StateKey.PLAN_FILE_PATH), "/tmp/sessions/s1/plans/plan-abc123.md"
        )

        block = _build_plan_mode_block(SESSION)

        assert "Approved Plan" in block
        assert "/tmp/sessions/s1/plans/plan-abc123.md" in block
        assert "enter_plan_mode(replan=True)" in block

    def test_ready_without_a_path_injects_nothing(self, _isolated_state):
        _isolated_state["mem"].set_state(SESSION, str(StateKey.PLAN_STATUS), "ready")

        assert _build_plan_mode_block(SESSION) == ""

    def test_a_broken_register_is_fail_open(self, monkeypatch):
        import workspace.prompt_builder as pb

        class _Boom:
            def get_state(self, *_a, **_k):
                raise RuntimeError("register down")

        monkeypatch.setattr("runtime.state_register_mem", _Boom())
        monkeypatch.setattr("runtime.state_register_db", _Boom())

        assert pb._build_plan_mode_block(SESSION) == ""


class TestPlanFilePersistence:
    def test_the_plan_file_is_session_scoped_and_revision_named(
        self, tmp_path, monkeypatch, _isolated_state
    ):
        plans = tmp_path / SESSION / "plans"
        monkeypatch.setattr("config.path.session_plans_dir", lambda _sid: plans)
        import agent.tools.plan_mode.exit_plan_mode as exit_mod

        monkeypatch.setattr(
            exit_mod, "interrupt", lambda _p: {"decisions": [{"type": "approve", "message": ""}]}
        )
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        plan = "## Goal\nship it\n\n## Steps\n1. do the thing — accept: tests pass\n"

        out = build_exit_plan_mode_tool()._run(plan, session_id=SESSION)

        files = list(plans.glob("plan-*.md"))
        assert len(files) == 1
        stored = files[0].read_text(encoding="utf-8")
        assert stored == plan.strip()
        revision = files[0].stem.removeprefix("plan-")
        assert len(revision) == 16
        assert str(files[0]) in out  # the approval reply names the saved file
        # The path lands in the register, which the approved-plan prompt block reads.
        assert _isolated_state["mem"].get_state(SESSION, str(StateKey.PLAN_FILE_PATH)) == str(
            files[0]
        )

    def test_resubmitting_the_same_plan_rewrites_the_same_file(self, tmp_path, monkeypatch):
        plans = tmp_path / SESSION / "plans"
        monkeypatch.setattr("config.path.session_plans_dir", lambda _sid: plans)
        import agent.tools.plan_mode.exit_plan_mode as exit_mod

        decisions = iter(
            [
                {"decisions": [{"type": "reject", "message": "tighten step 2"}]},
                {"decisions": [{"type": "approve", "message": ""}]},
            ]
        )
        monkeypatch.setattr(exit_mod, "interrupt", lambda _p: next(decisions))
        build_enter_plan_mode_tool()._run("complex", session_id=SESSION)
        exit_tool = build_exit_plan_mode_tool()
        plan = "## Goal\nsame plan text\n"

        exit_tool._run(plan, session_id=SESSION)
        exit_tool._run(plan, session_id=SESSION)

        # Same revision => same filename: the plan file does not multiply.
        assert len(list(plans.glob("plan-*.md"))) == 1
