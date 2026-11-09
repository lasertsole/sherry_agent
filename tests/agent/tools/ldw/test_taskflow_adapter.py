"""TaskFlow fallback adapter: seed successes, resume children, write journal."""

from __future__ import annotations

import pytest

import agent.tools.ldw.taskflow_adapter as adapter
from agent.tools.ldw.compiler import compile_plan_to_workflow

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Tool:
    def __init__(self, fn):
        self.coroutine = fn


class _Journal:
    def __init__(self):
        self.writes = []

    async def put(self, key, record):
        self.writes.append((key, record.result))


def _meta() -> dict:
    return compile_plan_to_workflow(
        {
            "plan_key": "k1",
            "flow_id": "flow-old",
            "steps": [
                {"step_id": "a", "task": "do a"},
                {"step_id": "b", "task": "do b", "depends_on": ["a"]},
            ],
        }
    )


def _flow(status_b: str) -> dict:
    steps = [
        {"step_id": "a", "task": "do a", "status": "done", "child_session_key": "synthetic"},
        {"step_id": "b", "task": "do b", "status": status_b, "depends_on": ["a"]},
    ]
    results = [
        {
            "child_session_key": "synthetic",
            "result": "A",
            "result_hash": "x",
            "step_id": "a",
        }
    ]
    if status_b in ("done", "failed", "dispatched"):
        steps[1]["child_session_key"] = "real-b"
        results.append(
            {
                "child_session_key": "real-b",
                "result": "B",
                "result_hash": "y",
                "step_id": "b",
            }
        )
    return {
        "flow_id": "flow-new",
        "status": "running",
        "state": {"steps": steps, "results": results},
    }


def _patch_store(monkeypatch, flows):
    state = {"iter": iter(flows)}

    def _get(flow_id, _sid):
        if flow_id == "flow-old":
            return None
        return next(state["iter"], None)

    monkeypatch.setattr(adapter.store_sqlite, "get_flow_sync", _get)


@pytest.mark.asyncio
async def test_seeds_successful_steps_and_dispatches_only_failed(monkeypatch) -> None:
    calls: list[tuple] = []
    journal = _Journal()

    async def _create(**kwargs):
        calls.append(("create", kwargs["initial_state"]))
        return "TaskFlow created"

    async def _dispatch(**kwargs):
        calls.append(("dispatch", kwargs["step_ids"]))
        return "dispatched"

    async def _wait(**kwargs):
        calls.append(("wait", kwargs["flow_id"]))
        return "settled"

    async def _resume(**kwargs):
        calls.append(("resume", kwargs["child_session_key"]))
        return "resumed"

    async def _finish(**kwargs):
        calls.append(("finish", kwargs["flow_id"]))
        return "done"

    _patch_store(
        monkeypatch,
        [_flow("ready"), _flow("dispatched"), _flow("dispatched"), _flow("done"), _flow("done")],
    )
    monkeypatch.setattr(adapter, "taskflow_create", _Tool(_create))
    monkeypatch.setattr(adapter, "taskflow_dispatch", _Tool(_dispatch))
    monkeypatch.setattr(adapter, "taskflow_wait_all", _Tool(_wait))
    monkeypatch.setattr(adapter, "taskflow_resume", _Tool(_resume))
    monkeypatch.setattr(adapter, "taskflow_finish", _Tool(_finish))
    monkeypatch.setattr(adapter, "_run_by_key", lambda _key: None)

    outcome = await adapter.run_taskflow_fallback(
        "sess", "k1", _meta(), {"a": "A"}, "variant", journal=journal, repair=False
    )

    assert outcome["status"] == "ok", outcome
    create_state = dict(calls[0][1])
    steps = create_state["steps"]
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "ready"
    assert ("dispatch", ["b"]) in calls
    assert ("resume", "real-b") in calls
    waited_id = next(cid for name, cid in calls if name == "wait")
    assert ("finish", waited_id) in calls
    assert [text for _, text in journal.writes] == ["A", "B"]


@pytest.mark.asyncio
async def test_failed_step_stays_partial_and_triggers_repair(monkeypatch) -> None:
    repaired: list[tuple] = []
    journal = _Journal()

    async def _create(**_kwargs):
        return "TaskFlow created"

    async def _dispatch(**_kwargs):
        return "dispatched"

    async def _wait(**_kwargs):
        return "settled"

    async def _resume(**_kwargs):
        return "resumed"

    async def _finish(**_kwargs):
        raise AssertionError("a failed flow must not finish")

    failed_flow = _flow("failed")
    failed_flow["state"]["steps"][1]["fail_reason"] = "boom"
    _patch_store(
        monkeypatch,
        [_flow("ready"), _flow("dispatched"), _flow("dispatched"), failed_flow, failed_flow],
    )
    monkeypatch.setattr(adapter, "taskflow_create", _Tool(_create))
    monkeypatch.setattr(adapter, "taskflow_dispatch", _Tool(_dispatch))
    monkeypatch.setattr(adapter, "taskflow_wait_all", _Tool(_wait))
    monkeypatch.setattr(adapter, "taskflow_resume", _Tool(_resume))
    monkeypatch.setattr(adapter, "taskflow_finish", _Tool(_finish))
    monkeypatch.setattr(adapter, "taskflow_fail", _Tool(lambda **_kw: _ok("failed")))
    monkeypatch.setattr(adapter, "_run_by_key", lambda _key: None)

    async def _repair(*args, **kwargs):
        repaired.append((args, kwargs))
        return {"merged": True}

    monkeypatch.setattr(adapter, "repair_extraction", _repair)

    outcome = await adapter.run_taskflow_fallback(
        "sess", "k1", _meta(), {"a": "A"}, "variant", journal=journal
    )

    assert outcome["status"] == "partial", outcome
    assert outcome["failed_steps"] == ["b"]
    assert outcome["repaired"] is True
    assert repaired


async def _ok(_value):
    return _value


def test_seeded_schema_result_carries_structured_result() -> None:
    meta = compile_plan_to_workflow(
        {
            "plan_key": "k1",
            "steps": [
                {
                    "step_id": "a",
                    "task": "do a",
                    "response_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    )
    state, _flow_id = adapter._initial_state(meta, {"a": '{"answer":"A"}'}, "v")
    record = state["results"][0]
    assert record["structured_result"] == {"answer": "A"}
    assert record["schema_validated"] is True
