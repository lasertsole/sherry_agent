"""The flat ``taskflow_updated`` payload the progress panel renders.

The contract under test:

* a flow's wire shape is a FLAT ordered step list plus per-status totals — no
  wave/level projection, because the scheduler unlocks each step on its own
  ``depends_on`` and the old grouping implied a round barrier that never existed;
* the payload carries what the panel needs — per-status counts, clipped task
  lines, and the flow identity — and it never invents state the flow does not
  have;
* the same shape serves the push and the ``taskflow_refresh`` snapshot.
"""

from __future__ import annotations

import json

import pytest

from agent.tools.taskflow.progress_push import (
    STATUS_ORDER,
    flow_progress,
    progress_payload,
    push_taskflow_progress,
)

pytestmark = [pytest.mark.unit]


def _step(
    step_id: str, *, deps: list[str] | None = None, status: str = "ready", task: str = ""
) -> dict:
    return {
        "step_id": step_id,
        "task": task or f"task {step_id}",
        "depends_on": deps or [],
        "status": status,
    }


def _flow(steps: list[dict], *, description: str = "demo") -> dict:
    return {
        "flow_id": "flow-1",
        "status": "active",
        "state": {"description": description, "steps": steps},
    }


# ── flow_progress ────────────────────────────────────────────────────────────


def test_flow_progress_is_a_flat_step_list_in_flow_order() -> None:
    progress = flow_progress(
        _flow(
            [
                _step("a", status="done"),
                _step("b", status="done"),
                _step("c", deps=["a", "b"], status="dispatched"),
                _step("d", deps=["c"], status="blocked"),
            ]
        )
    )

    assert progress["total"] == 4
    assert progress["done"] == 2
    assert [step["step_id"] for step in progress["steps"]] == ["a", "b", "c", "d"]
    assert [step["status"] for step in progress["steps"]] == [
        "done",
        "done",
        "dispatched",
        "blocked",
    ]
    assert progress["by_status"]["blocked"] == 1
    assert set(progress["by_status"]) == set(STATUS_ORDER)
    assert "waves" not in progress and "current_wave" not in progress


def test_flow_progress_preserves_dependency_metadata_only_for_the_store() -> None:
    """``depends_on`` is scheduling data; the wire row carries no DAG projection."""
    progress = flow_progress(_flow([_step("b", deps=["a"], status="ready")]))

    assert progress["steps"] == [{"step_id": "b", "task": "task b", "status": "ready"}]


def test_flow_progress_without_steps_is_empty_not_missing() -> None:
    progress = flow_progress({"flow_id": "f", "status": "active", "state": {}})

    assert progress["steps"] == []
    assert progress["total"] == 0
    assert progress["by_status"]["done"] == 0


def test_flow_progress_clips_long_text_and_carries_the_identity() -> None:
    progress = flow_progress(_flow([_step("a", task="x" * 500)], description="d" * 500))

    assert len(progress["description"]) == 200
    assert len(progress["steps"][0]["task"]) == 200
    assert progress["flow_id"] == "flow-1"
    assert progress["status"] == "active"


def test_flow_progress_normalizes_missing_status_and_id() -> None:
    progress = flow_progress(_flow([{"task": "no id or status"}]))

    # A malformed step is still represented; the store validates real writes.
    assert progress["steps"] == [{"step_id": "", "task": "no id or status", "status": "ready"}]


# ── progress_payload ─────────────────────────────────────────────────────────


def test_payload_aggregates_the_sessions_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    flows = [
        _flow([_step("a", status="done"), _step("b", deps=["a"], status="ready")]),
        {
            "flow_id": "flow-2",
            "status": "active",
            "state": {"description": "second", "steps": [_step("x", status="done")]},
        },
    ]
    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", lambda sid: flows)

    payload = progress_payload("sess-1")

    assert payload["totals"] == {"flows": 2, "total": 3, "done": 2}
    assert [flow["flow_id"] for flow in payload["flows"]] == ["flow-1", "flow-2"]
    assert [step["step_id"] for step in payload["flows"][0]["steps"]] == ["a", "b"]


def test_payload_without_flows_is_an_empty_board(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", lambda sid: [])

    payload = progress_payload("sess-none")

    assert payload["flows"] == []
    assert payload["totals"] == {"flows": 0, "total": 0, "done": 0}


# ── push_taskflow_progress ───────────────────────────────────────────────────


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.asyncio
async def test_push_sends_the_frame_to_the_sessions_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.tools.taskflow import progress_push

    socket = _FakeWebSocket()
    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register, "get_websockets_by_session_id", lambda sid: [socket]
    )
    monkeypatch.setattr(
        progress_push.store_sqlite,
        "get_active_flows_sync",
        lambda sid: [_flow([_step("a", status="done")])],
    )

    await push_taskflow_progress("sess-1")

    assert len(socket.sent) == 1
    frame = json.loads(socket.sent[0])
    assert frame["event"] == "taskflow_updated"
    assert frame["session_id"] == "sess-1"
    assert frame["content"]["totals"]["done"] == 1
    assert frame["content"]["flows"][0]["steps"][0]["step_id"] == "a"


@pytest.mark.asyncio
async def test_push_is_a_quiet_no_op_without_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(ws_push.relation_register, "get_websockets_by_session_id", lambda sid: [])
    called: list[str] = []
    monkeypatch.setattr(
        progress_push.store_sqlite, "get_active_flows_sync", lambda sid: called.append(sid) or []
    )

    await push_taskflow_progress("sess-1")
    await push_taskflow_progress("   ")

    # No socket ⇒ never even read the store; a blank session is skipped outright.
    assert called == []


@pytest.mark.asyncio
async def test_push_swallows_a_failing_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    class _Boom:
        async def send_text(self, text: str) -> None:
            raise RuntimeError("socket gone")

    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register, "get_websockets_by_session_id", lambda sid: [_Boom()]
    )
    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", lambda sid: [])

    # Must not raise: a send failure can never break the tool that mutated the flow.
    await push_taskflow_progress("sess-1")


# ── wiring: a real mutation pushes, and the refresh answers the same shape ───


@pytest.mark.asyncio
async def test_a_real_step_update_pushes_a_frame(
    isolated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not the helper: editing a flow's steps reaches the socket.

    Uses the tool path end to end (store write → shared push), with only the
    websocket and the dependency registry stubbed.
    """
    import importlib

    update_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_update_steps")
    from agent.tools.taskflow.registry import store_sqlite

    # A done step must carry its child_session_key (the store's own contract), so
    # the fixture stays at "ready" and the update adds the dependent step.
    await store_sqlite.create_flow(
        "flow-wire",
        {"description": "wire probe", "steps": [_step("a", status="ready")], "results": []},
        session_id="sess-wire",
        status="running",
    )
    socket = _FakeWebSocket()
    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register, "get_websockets_by_session_id", lambda sid: [socket]
    )

    result = await update_mod.taskflow_update_steps.coroutine(
        flow_id="flow-wire",
        steps=[
            _step("a", status="ready"),
            _step("b", deps=["a"], status="ready"),
        ],
        session_id="sess-wire",
    )
    assert result.startswith("TaskFlow steps updated"), result

    assert len(socket.sent) == 1, socket.sent
    frame = json.loads(socket.sent[0])
    assert frame["event"] == "taskflow_updated"
    assert frame["content"]["flows"][0]["steps"][1]["step_id"] == "b"
    assert frame["content"]["totals"]["total"] == 2


@pytest.mark.asyncio
async def test_the_refresh_handler_answers_the_pushed_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.tools.taskflow import progress_push
    from server.trigger.core import taskflow_refresh_processor

    monkeypatch.setattr(
        progress_push.store_sqlite,
        "get_active_flows_sync",
        lambda sid: [_flow([_step("a", status="done")])],
    )

    reply = await taskflow_refresh_processor("sess-r", "")

    assert reply is not None
    assert reply["event"] == "taskflow_updated"
    assert reply["session_id"] == "sess-r"
    assert reply["content"]["totals"]["done"] == 1


@pytest.mark.asyncio
async def test_the_refresh_handler_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push
    from server.trigger.core import taskflow_refresh_processor

    def _boom(sid: str) -> list:
        raise RuntimeError("store gone")

    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", _boom)

    assert await taskflow_refresh_processor("sess-r", "") is None
