"""SpawnRequest (2026-11-09 audit H4): one immutable object for a spawn.

The public entry point still accepts the historical keyword arguments through
``from_legacy`` (compatibility), but the orchestration receives ONLY the
request. These tests pin the defaults and the adapter contract.
"""

from __future__ import annotations

import inspect

import pytest

from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.request import SpawnRequest

pytestmark = [pytest.mark.unit]


def test_request_defaults_match_the_historical_signature():
    request = SpawnRequest(task="t", requester_session_key="parent")

    assert request.agent_id == "main"
    assert request.cleanup == "delete"
    assert request.expects_completion_message is True
    assert request.isolation is False
    assert request.task_name is None
    assert request.output_schema is None
    assert request.flow_id is None
    assert request.step_id is None


def test_from_legacy_builds_the_same_object_as_direct_construction():
    fields = {
        "task": "t",
        "requester_session_key": "parent",
        "agent_id": "worker",
        "cleanup": "keep",
        "isolation": True,
        "cwd": "/tmp/project",
        "flow_id": "flow-1",
        "step_id": "s1",
    }

    assert SpawnRequest.from_legacy(**fields) == SpawnRequest(**fields)


def test_from_legacy_rejects_unknown_names():
    with pytest.raises(TypeError, match="unexpected keyword"):
        SpawnRequest.from_legacy(task="t", requester_session_key="p", model_override="x")


def test_public_entrypoint_is_metadata_thin():
    params = inspect.signature(spawn_core.spawn_subagent_direct).parameters
    assert "request" in params
    assert "legacy" in params
    assert "task" not in params
    assert len(params) == 2


@pytest.mark.asyncio
async def test_kwargs_adapter_hands_the_core_one_request(monkeypatch):
    seen: list[SpawnRequest] = []

    async def fake_core(request: SpawnRequest):
        seen.append(request)
        return spawn_core.SpawnResult(status="accepted", run_id="r1")

    monkeypatch.setattr(spawn_core, "_spawn_subagent", fake_core)

    result = await spawn_core.spawn_subagent_direct(
        task="do it", requester_session_key="parent", label="L"
    )

    assert result.status == "accepted"
    assert len(seen) == 1
    assert seen[0].task == "do it"
    assert seen[0].label == "L"


@pytest.mark.asyncio
async def test_passing_both_forms_is_a_caller_error(monkeypatch):
    async def fake_core(request: SpawnRequest):  # pragma: no cover - must not run
        raise AssertionError("core must not run")

    monkeypatch.setattr(spawn_core, "_spawn_subagent", fake_core)

    with pytest.raises(TypeError, match="OR keyword arguments"):
        await spawn_core.spawn_subagent_direct(
            SpawnRequest(task="t", requester_session_key="p"), task="t"
        )
