"""Expert-panel dispatch: a step naming a registered expert reaches its child
with the expert's persona, merged skills and spawn overrides.

The seam is the same ``_dispatch.dispatch_child`` fake the dispatch tests use;
the registry is a temp ``EXPERTS_DIR`` so no real definitions are touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.expert_registry import ExpertConfig, save_expert
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools._retry import step_spawn_kwargs
from agent.tools.taskflow.tools._shared import new_step
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch

pytestmark = [pytest.mark.unit]

_SESSION = "sess-expert"


@pytest.fixture
def registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "experts"
    monkeypatch.setattr("agent.tools.expert_registry.EXPERTS_DIR", target)
    save_expert(
        ExpertConfig(
            name="frontend-expert",
            description="Frontend specialist",
            persona_prompt="You are a frontend expert.",
            model="claude-frontend",
            skills=["code_wiki"],
            step_timeout_seconds=1800.0,
            goal_max_turns=1,
        )
    )
    return target


def _recording_dispatch(calls: list, keys: list[str]):
    async def _fake(task: str, requester_session_key: str, label: str | None = None, **kwargs):
        calls.append({"task": task, "kwargs": kwargs})
        return keys[len(calls) - 1]

    return _fake


async def _seed(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="expert dispatch probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_step_naming_an_expert_dispatches_its_config(
    registry_dir: Path, monkeypatch: pytest.MonkeyPatch, isolated_db: Path
):
    calls: list = []
    monkeypatch.setattr(
        _dispatch, "dispatch_child", _recording_dispatch(calls, ["agent:main:subagent:c1"])
    )
    step = new_step(
        "step-1",
        "build the frontend",
        status=StepStatus.READY,
        expert="frontend-expert",
        skills=["browser"],
    )
    await _seed("flow-1", [step])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )
    assert "Error" not in out, out

    call = calls[0]
    kwargs = call["kwargs"]
    assert kwargs["step_persona"] == "You are a frontend expert."
    assert kwargs["step_skills_whitelist"] == ["code_wiki"]
    assert kwargs["step_model"] == "claude-frontend"
    assert kwargs["step_timeout_seconds"] == 1800.0
    assert kwargs["step_goal_max_turns"] == 1
    # The expert's skills merge into the step's own binding for the SKILL.md prepend.
    assert "code_wiki" in call["task"]
    assert "browser" in call["task"]


@pytest.mark.asyncio
async def test_step_level_values_win_over_expert_defaults(
    registry_dir: Path, monkeypatch: pytest.MonkeyPatch, isolated_db: Path
):
    calls: list = []
    monkeypatch.setattr(
        _dispatch, "dispatch_child", _recording_dispatch(calls, ["agent:main:subagent:c1"])
    )
    step = new_step(
        "step-1",
        "task",
        status=StepStatus.READY,
        expert="frontend-expert",
        step_model="my-override",
        persona_prompt="step persona wins",
    )
    await _seed("flow-1", [step])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )
    assert "Error" not in out, out

    kwargs = calls[0]["kwargs"]
    assert kwargs["step_model"] == "my-override"
    assert kwargs["step_persona"] == "step persona wins"


@pytest.mark.asyncio
async def test_unknown_expert_fails_that_dispatch_only(
    registry_dir: Path, monkeypatch: pytest.MonkeyPatch, isolated_db: Path
):
    calls: list = []
    monkeypatch.setattr(
        _dispatch, "dispatch_child", _recording_dispatch(calls, ["agent:main:subagent:c1"])
    )
    steps = [
        new_step("step-1", "ghost step", status=StepStatus.READY, expert="ghost"),
        new_step("step-2", "plain step", status=StepStatus.READY),
    ]
    await _seed("flow-1", steps)

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    assert "ghost" in out and "Error" in out
    assert len(calls) == 1  # step-2 still dispatched
    flow = await store_sqlite.get_flow("flow-1", _SESSION)
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert by_id["step-2"]["status"] == StepStatus.DISPATCHED


def test_new_step_accepts_expert_fields():
    step = new_step(
        "s",
        "t",
        expert="frontend-expert",
        persona_prompt="p",
        skills_whitelist=["a"],
        goal_max_turns=1,
    )
    assert step["expert"] == "frontend-expert"
    assert step["persona_prompt"] == "p"
    assert step["skills_whitelist"] == ["a"]


def test_step_spawn_kwargs_carries_expert_fields():
    step = {"persona_prompt": "p", "skills_whitelist": ["a"], "goal_max_turns": 1, "step_id": "s1"}

    kwargs = step_spawn_kwargs(step, flow_id="f1")

    assert kwargs["step_persona"] == "p"
    assert kwargs["step_skills_whitelist"] == ["a"]
    assert kwargs["step_goal_max_turns"] == 1
