"""TaskFlow auto-resume: an announce resumes the flow that dispatched the child.

The loop-closure half of the announce pipeline (plan: TaskFlow 循环闭环, 方案一):
a child spawned by a TaskFlow step has its result injected back into the flow
before the human-facing announcement goes out — and the flow's own
``auto_dispatch`` then starts the wave it just unlocked. Fail-open in both
directions: no owning flow, or a failing resume, keeps today's behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.tools.subagent.announce import delivery as delivery_mod

pytestmark = [pytest.mark.unit]


def _run(child_key: str = "agent:main:subagent:c1", result: str = "step result"):
    return SimpleNamespace(
        run_id="run-1",
        child_session_key=child_key,
        completion=SimpleNamespace(result_text=result, required=True),
    )


class _FakeResume:
    """Stands in for the ``taskflow_resume`` tool object (only ``coroutine`` is used)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

        async def coroutine(**kwargs) -> str:
            self.calls.append(kwargs)
            return "TaskFlow resumed: flow_id=flow-1, unlocked=[step-2]"

        self.coroutine = coroutine


@pytest.fixture()
def flow_store(monkeypatch):
    class _Store:
        def __init__(self, flow):
            self.flow = flow

        async def get_flow_for_child_session(self, key):
            return self.flow

    return _Store({"flow_id": "flow-1", "state": {}})


@pytest.mark.asyncio
async def test_resume_runs_for_a_flow_owned_child(monkeypatch, flow_store):
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    fake_resume = _FakeResume()
    monkeypatch.setattr(
        taskflow_store, "get_flow_for_child_session", flow_store.get_flow_for_child_session
    )
    import importlib

    # The package re-exports the StructuredTool under the module's own name, so
    # the module must be imported by path (that is the attribute the delivery
    # helper resolves).
    resume_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_resume")
    monkeypatch.setattr(resume_mod, "taskflow_resume", fake_resume)

    resumed = await delivery_mod._try_auto_resume_taskflow(_run())

    assert resumed is True
    assert fake_resume.calls[0]["flow_id"] == "flow-1"
    assert fake_resume.calls[0]["child_session_key"] == "agent:main:subagent:c1"
    assert fake_resume.calls[0]["result"] == "step result"


@pytest.mark.asyncio
async def test_no_owning_flow_keeps_manual_behaviour(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    async def _none(key):
        return None

    monkeypatch.setattr(taskflow_store, "get_flow_for_child_session", _none)

    assert await delivery_mod._try_auto_resume_taskflow(_run()) is False


@pytest.mark.asyncio
async def test_a_failing_resume_is_fail_open(monkeypatch):
    import importlib

    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    resume_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_resume")

    async def _flow(key):
        return {"flow_id": "flow-1"}

    monkeypatch.setattr(taskflow_store, "get_flow_for_child_session", _flow)

    class _Boom:
        async def coroutine(self, **kwargs):
            raise RuntimeError("registry down")

    monkeypatch.setattr(resume_mod, "taskflow_resume", _Boom())

    assert await delivery_mod._try_auto_resume_taskflow(_run()) is False
