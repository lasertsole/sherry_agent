"""Force-close the provider client pool after subagent timeout/cancel.

``asyncio.wait_for`` raises TimeoutError / CancelledError (BaseException), which
bypass ``LLMRetryMiddleware``'s ``except Exception`` — so ``reset_async_client``
never fires and a stale keep-alive connection can survive into a later
continuation turn that reuses the same child agent. These tests lock the seam:
``_build_child_agent`` exposes the model, and every timeout/cancel catch calls
the fail-open reset helper.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.tools.subagent.spawn.lifecycle import reset_child_provider_client

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def create_agent_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub ``_build_child_agent``'s heavy deps and capture its create_agent call."""
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(name="child-graph")

    class _StubCheckpointer:
        async def setup(self) -> None:
            return None

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    class _StubLLM:
        def bind(self, **kwargs):
            return SimpleNamespace(kind="bound", kwargs=kwargs)

        def with_request_options(self, options):
            return SimpleNamespace(kind="bound", kwargs=options)

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: _StubLLM())
    monkeypatch.setattr("models.build_main_llm_for_profile", lambda **k: _StubLLM())
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: _StubLLM())
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "200000")
    return captured


class _ResetModel:
    def __init__(self, result: bool = True, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.result = result
        self.raises = raises

    async def reset_async_client(self) -> bool:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


def _child(model: object | None) -> SimpleNamespace:
    """Minimal stand-in for the child agent carrying the exposed model."""
    child = SimpleNamespace()
    if model is not None:
        child._sherry_model = model
    return child


# ── 1. model exposure ───────────────────────────────────────────────────────


def test_child_agent_exposes_model(create_agent_capture):
    """_build_child_agent stores the model handed to create_agent on the wrapper."""
    from agent.tools.subagent.spawn.child_agent_factory import _build_child_agent
    from agent.tools.subagent.types.registry import SubagentSessionRole

    agent = asyncio.run(
        _build_child_agent(
            system_prompt="child",
            tools=[],
            tool_allow=None,
            tool_deny=None,
            role=SubagentSessionRole.LEAF,
        )
    )
    model = getattr(agent, "_sherry_model", None)
    assert model is not None
    # It is the very model passed to create_agent (the captured `model` kwarg).
    assert model is create_agent_capture["model"]


# ── 2-5. helper behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_after_timeout_calls_reset_async_client():
    model = _ResetModel(result=True)
    await reset_child_provider_client(_child(model))
    assert model.calls == 1


@pytest.mark.asyncio
async def test_reset_fail_open_without_model_or_method():
    # no _sherry_model at all
    await reset_child_provider_client(_child(None))
    # model present but not resettable
    await reset_child_provider_client(_child(object()))
    # reset raises — must not propagate
    await reset_child_provider_client(_child(_ResetModel(raises=RuntimeError("boom"))))
    # reset returns False — must not propagate
    await reset_child_provider_client(_child(_ResetModel(result=False)))


@pytest.mark.asyncio
async def test_reset_is_fail_open_for_none_child():
    await reset_child_provider_client(None)


# ── 6. catch paths invoke the helper ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_called_on_goal_loop_timeout(monkeypatch: pytest.MonkeyPatch):
    """A real wait_for timeout in the goal loop invokes the reset helper."""
    import uuid

    import agent.tools.subagent.registry.lifecycle as registry_lifecycle
    import agent.tools.subagent.spawn.core as spawn_core
    import agent.tools.subagent.spawn.lifecycle as spawn_lifecycle
    import agent.tools.taskflow.evidence_collector as evidence_collector
    from agent.tools.subagent.types.registry import SubagentRunRecord
    from langchain_core.messages import HumanMessage

    model = _ResetModel(result=True)
    child = SimpleNamespace(_sherry_model=model)

    async def _slow_ainvoke(input, config):
        await asyncio.sleep(5)
        return {"messages": [HumanMessage(content="late")]}

    child.ainvoke = _slow_ainvoke

    async def _fake_build(**_):
        return child

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(registry_lifecycle, "complete_subagent_run", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_release_child_resources", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_capture_child_transcript", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_kill_descendants_after_failure", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)
    monkeypatch.setattr(spawn_lifecycle, "reset_child_provider_client", reset_child_provider_client)

    run = SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=f"agent:main:subagent:{uuid.uuid4().hex[:12]}",
        requester_session_key="agent:main:session:test",
        task="t",
        spawned_by="agent:main:session:test",
    )
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message="t",
        tools=[],
        timeout_seconds=0.05,
        goal_max_turns=1,
    )
    assert model.calls == 1, "timeout must force-close the child's provider client"


@pytest.mark.asyncio
async def test_reset_called_on_cancel(monkeypatch: pytest.MonkeyPatch):
    """An external cancel of the child invoke invokes the reset helper."""
    import uuid

    import agent.tools.subagent.registry.lifecycle as registry_lifecycle
    import agent.tools.subagent.spawn.core as spawn_core
    import agent.tools.subagent.spawn.lifecycle as spawn_lifecycle
    import agent.tools.taskflow.evidence_collector as evidence_collector
    from agent.tools.subagent.types.registry import SubagentRunRecord
    from langchain_core.messages import HumanMessage

    model = _ResetModel(result=True)
    child = SimpleNamespace(_sherry_model=model)
    started = asyncio.Event()

    async def _cancelled_ainvoke(input, config):
        started.set()
        await asyncio.sleep(5)
        return {"messages": [HumanMessage(content="late")]}

    child.ainvoke = _cancelled_ainvoke

    async def _fake_build(**_):
        return child

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(registry_lifecycle, "complete_subagent_run", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_release_child_resources", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_capture_child_transcript", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_kill_descendants_after_failure", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)

    run = SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=f"agent:main:subagent:{uuid.uuid4().hex[:12]}",
        requester_session_key="agent:main:session:test",
        task="t",
        spawned_by="agent:main:session:test",
    )

    async def _drive():
        await spawn_core._execute_subagent(
            run=run,
            system_prompt="sys",
            user_message="t",
            tools=[],
            timeout_seconds=0.0,
            goal_max_turns=1,
        )

    task = asyncio.create_task(_drive())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert model.calls == 1, "cancel must force-close the child's provider client"


@pytest.mark.asyncio
async def test_reset_called_on_steer_timeout():
    """The steer/restart path also force-closes the provider client pool."""
    from agent.tools.subagent.config import get_config, set_config
    from agent.tools.subagent.control.steer import _execute_steered_subagent
    from agent.tools.subagent.registry import (
        clear as clear_registry,
        get_run,
        register_run,
        set_run,
    )

    class _SlowAgent:
        def __init__(self, model):
            self._sherry_model = model

        async def ainvoke(self, input, config):
            await asyncio.sleep(0.3)
            return {"messages": [{"content": "late"}]}

    model = _ResetModel(result=True)
    orig = get_config()
    set_config(orig.model_copy(update={"run_timeout_seconds": 300.0}))
    clear_registry()
    try:
        run = register_run(
            child_session_key="agent:main:subagent:client-reset-steer",
            requester_session_key="agent:main:session:test",
            task="t",
            depth=1,
        )
        set_run(run)
        await _execute_steered_subagent(run, _SlowAgent(model), "[STEER] go", 0.05)
        got = get_run(run.run_id)
        assert (
            str(getattr(got.execution.outcome.status, "value", got.execution.outcome.status))
            == "timeout"
        )
        assert model.calls == 1
    finally:
        set_config(orig)
        clear_registry()


@pytest.mark.asyncio
async def test_reset_not_called_on_normal_exit(monkeypatch: pytest.MonkeyPatch):
    """A clean run does not force-close the client pool."""
    import uuid

    import agent.tools.subagent.registry.lifecycle as registry_lifecycle
    import agent.tools.subagent.spawn.completion_judge as completion_judge
    import agent.tools.subagent.spawn.core as spawn_core
    import agent.tools.subagent.spawn.lifecycle as spawn_lifecycle
    import agent.tools.taskflow.evidence_collector as evidence_collector
    from agent.tools.subagent.types.registry import SubagentRunRecord
    from langchain_core.messages import HumanMessage

    model = _ResetModel(result=True)
    child = SimpleNamespace(_sherry_model=model)

    async def _ok_ainvoke(input, config):
        return {"messages": [HumanMessage(content="done")]}

    child.ainvoke = _ok_ainvoke

    async def _fake_build(**_):
        return child

    async def _noop(*_, **__):
        return None

    async def _judge(**_):
        from agent.tools.subagent.spawn.completion_judge import (
            CompletionJudgeResult,
            CompletionVerdict,
        )

        return CompletionJudgeResult(CompletionVerdict.DONE, "done", "")

    monkeypatch.setattr(spawn_core, "_build_child_agent", _fake_build)
    monkeypatch.setattr(spawn_core, "fire_progress_hook", _noop)
    monkeypatch.setattr(spawn_core, "fire_ended_hook", _noop)
    monkeypatch.setattr(registry_lifecycle, "complete_subagent_run", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_release_child_resources", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_capture_child_transcript", _noop)
    monkeypatch.setattr(spawn_lifecycle, "_kill_descendants_after_failure", _noop)
    monkeypatch.setattr(evidence_collector, "collect_evidence_summary", lambda **_: None)
    monkeypatch.setattr(completion_judge, "judge_completion", _judge)

    run = SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        child_session_key=f"agent:main:subagent:{uuid.uuid4().hex[:12]}",
        requester_session_key="agent:main:session:test",
        task="t",
        spawned_by="agent:main:session:test",
    )
    await spawn_core._execute_subagent(
        run=run,
        system_prompt="sys",
        user_message="t",
        tools=[],
        timeout_seconds=1.0,
        goal_max_turns=5,
    )
    assert model.calls == 0
