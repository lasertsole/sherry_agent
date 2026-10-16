"""End-to-end: the judgement gate's retry loop against the REAL pipeline.

The hermetic unit tests pin the pieces with ``_dispatch.dispatch_child``
monkeypatched. This file strings the whole loop together with only the
SUBAGENT lane faked (the repo's ``real_child_dispatch`` idiom), so every hop
runs for real:

``taskflow_run_task`` (schema + retry policy on the step)
  → the real spawn pipeline (planning, prompt assembly, run record)
  → the child's system prompt carries the schema text
  → the child reports a non-conforming result
  → ``taskflow_resume``'s Tier 1 rejects it
  → the retry re-dispatches through the real pipeline, and the replacement
    child's task carries the validator's message while its spawn parameters
    (schema / role / model / timeout) come from the step
  → the replacement reports a conforming result and the step lands done.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_SESSION = "sess-gate-e2e"
_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["status", "count"],
    "additionalProperties": False,
}


@pytest.fixture()
def _captured_lane(monkeypatch: pytest.MonkeyPatch):
    """Fake the SUBAGENT lane; everything upstream of it runs for real."""
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


def _tools() -> dict:
    return {tool.name: tool for tool in build_taskflow_tools()}


async def _run_task(**kwargs) -> str:
    return await _tools()["taskflow_run_task"].coroutine(session_id=_SESSION, **kwargs)


async def _resume(**kwargs) -> str:
    return await _tools()["taskflow_resume"].coroutine(session_id=_SESSION, **kwargs)


@pytest.mark.asyncio
async def test_the_whole_gate_retry_loop_runs_on_the_real_pipeline(
    isolated_db: Path, _captured_lane, real_child_dispatch
):
    # Given a flow whose step declares a schema, a retry policy and a spawn
    # contract (run_task registers the step and dispatches it in one call)
    created = await _tools()["taskflow_create"].coroutine(
        session_id=_SESSION, flow_id="flow-e2e", description="gate e2e"
    )
    assert "Error" not in created, created
    out = await _run_task(
        flow_id="flow-e2e",
        task="report the status",
        response_schema=_SCHEMA,
        retry_policy={"max_retries": 2, "retry_delay_seconds": 0.0, "retry_on": []},
        functional_role="reviewer",
        step_model="deepseek-flash",
        step_timeout_seconds=1800.0,
    )
    assert "dispatched" in out, out
    await asyncio.sleep(0.05)
    assert len(_captured_lane) == 1, "the first dispatch went through the real pipeline"

    # …the child sees the schema in BOTH places: as a spawn parameter (the
    # child agent's response_format is built from it) and as prompt text.
    first = _captured_lane[0]
    assert first["output_schema"] == _SCHEMA, "the schema rides into the child executor"
    assert "count" in first["system_prompt"], "the prompt-level schema text is present"

    flow = await store_sqlite.get_flow("flow-e2e", _SESSION)
    assert flow is not None
    child_key = flow["state"]["steps"][0]["child_session_key"]
    assert child_key

    # When the child reports JSON that misses a required field
    out = await _resume(
        flow_id="flow-e2e",
        child_session_key=child_key,
        result=json.dumps({"status": "ok"}),
    )

    # Then Tier 1 caught it and the REAL pipeline spawned a replacement whose
    # task teaches the mistake while inheriting the step's spawn contract.
    assert "re-dispatched" in out, out
    await asyncio.sleep(0.05)
    assert len(_captured_lane) == 2, "the retry went through the real spawn pipeline"
    replacement = _captured_lane[1]
    assert "missing required field 'count'" in replacement["user_message"], replacement[
        "user_message"
    ]
    assert "## Previous Attempt Feedback" in replacement["user_message"]
    assert replacement["output_schema"] == _SCHEMA
    assert replacement["model_override"] == "deepseek-flash"
    assert replacement["timeout_seconds"] == 1800.0

    # And the replacement's own run record carries the reviewer role the step
    # declared (the whitelist/prompt half of the functional-role contract).
    from agent.tools.subagent.registry.queries import get_run_by_child_session_key

    refreshed = await store_sqlite.get_flow("flow-e2e", _SESSION)
    assert refreshed is not None
    retry_key = refreshed["state"]["steps"][0]["child_session_key"]
    assert retry_key != child_key, "the step now points at the replacement"
    run = get_run_by_child_session_key(retry_key)
    assert run is not None, "the replacement has its own run record"
    assert str(getattr(run, "functional_role", "")) in ("reviewer", "FunctionalRole.REVIEWER")

    # When the replacement reports a conforming result
    out = await _resume(
        flow_id="flow-e2e",
        child_session_key=retry_key,
        result=json.dumps({"status": "ok", "count": 3}),
    )

    # Then the step lands done, nothing else spawns, and the schema verdict is
    # recorded on the result.
    assert "done=1" in out and "failed=0" in out, out
    assert "schema: validated" in out, out
    await asyncio.sleep(0.05)
    assert len(_captured_lane) == 2, "a compliant result spawns nothing"

    final = await store_sqlite.get_flow("flow-e2e", _SESSION)
    assert final is not None
    step = final["state"]["steps"][0]
    assert step["status"] == "done"
    assert step["retry_count"] == 1
    results = final["state"]["results"]
    assert [r["schema_validated"] for r in results] == [False, True]
