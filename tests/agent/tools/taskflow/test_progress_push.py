"""DAG waves + the ``taskflow_updated`` payload the progress panel renders.

The contract under test:

* waves are longest-path levels: a step lands one wave after its deepest
  dependency, and dependencies that are not in the step list are ignored (they can
  never be waited on);
* a dependency cycle is reported in a trailing wave flagged ``cyclic`` instead of
  looping or dropping the steps silently;
* the payload carries what the panel needs — per-wave counts, the first wave with
  open work (``current_wave``), per-status totals and a clipped task line — and it
  never invents state the flow does not have.
"""

from __future__ import annotations

from agent.tools.taskflow.progress_push import flow_progress, progress_payload
from agent.tools.taskflow.waves import STATUS_ORDER, compute_waves

import pytest

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


# ── compute_waves ────────────────────────────────────────────────────────────


def test_independent_steps_share_the_first_wave() -> None:
    waves = compute_waves([_step("a"), _step("b"), _step("c")])

    assert [wave["total"] for wave in waves] == [3]
    assert waves[0]["index"] == 1


def test_a_step_lands_one_wave_after_its_deepest_dependency() -> None:
    steps = [_step("a"), _step("b", deps=["a"]), _step("c", deps=["a", "b"])]

    waves = compute_waves(steps)

    assert [[s["step_id"] for s in wave["steps"]] for wave in waves] == [["a"], ["b"], ["c"]]
    assert [wave["index"] for wave in waves] == [1, 2, 3]


def test_two_branches_of_different_depth_stay_on_their_own_levels() -> None:
    steps = [
        _step("root"),
        _step("short", deps=["root"]),
        _step("long-1", deps=["root"]),
        _step("long-2", deps=["long-1"]),
        _step("join", deps=["short", "long-2"]),
    ]

    waves = compute_waves(steps)

    assert [[s["step_id"] for s in wave["steps"]] for wave in waves] == [
        ["root"],
        ["short", "long-1"],
        ["long-2"],
        ["join"],
    ]


def test_dependencies_outside_the_list_are_ignored() -> None:
    waves = compute_waves([_step("a", deps=["ghost"])])

    assert len(waves) == 1
    assert waves[0]["steps"][0]["step_id"] == "a"


def test_self_and_mutual_dependencies_are_reported_as_cyclic() -> None:
    steps = [_step("a", deps=["b"]), _step("b", deps=["a"]), _step("solo")]

    waves = compute_waves(steps)

    assert len(waves) == 2
    assert waves[0]["steps"][0]["step_id"] == "solo"
    assert waves[1]["cyclic"] is True
    assert {s["step_id"] for s in waves[1]["steps"]} == {"a", "b"}


def test_wave_counts_track_statuses() -> None:
    steps = [
        _step("a", status="done"),
        _step("b", status="failed"),
        _step("c", deps=["a"], status="dispatched"),
    ]

    waves = compute_waves(steps)

    assert (waves[0]["total"], waves[0]["done"]) == (2, 1)
    assert waves[0]["by_status"]["failed"] == 1
    assert set(waves[0]["by_status"]) == set(STATUS_ORDER)
    assert (waves[1]["total"], waves[1]["done"]) == (1, 0)


def test_empty_and_malformed_inputs_are_safe() -> None:
    assert compute_waves([]) == []
    assert compute_waves([{}, {"step_id": ""}]) == []
    assert compute_waves([{"step_id": "a"}])[0]["steps"][0]["status"] == "ready"


# ── flow_progress ────────────────────────────────────────────────────────────


def test_flow_progress_points_at_the_first_wave_with_open_work() -> None:
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
    assert progress["current_wave"] == 2
    assert [wave["index"] for wave in progress["waves"]] == [1, 2, 3]
    assert progress["by_status"]["blocked"] == 1


def test_flow_progress_reports_zero_current_wave_once_everything_is_done() -> None:
    progress = flow_progress(
        _flow([_step("a", status="done"), _step("b", deps=["a"], status="done")])
    )

    assert progress["current_wave"] == 0
    assert progress["done"] == progress["total"] == 2


def test_flow_progress_without_steps_is_empty_not_missing() -> None:
    progress = flow_progress({"flow_id": "f", "status": "active", "state": {}})

    assert progress["waves"] == []
    assert progress["total"] == 0
    assert progress["current_wave"] == 0


def test_flow_progress_clips_long_text_and_carries_the_identity() -> None:
    progress = flow_progress(_flow([_step("a", task="x" * 500)], description="d" * 500))

    assert len(progress["description"]) == 200
    assert len(progress["waves"][0]["steps"][0]["task"]) == 200
    assert progress["flow_id"] == "flow-1"
    assert progress["status"] == "active"


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

    assert payload["totals"] == {
        "flows": 2,
        "total": 3,
        "done": 2,
        "current_wave": 2,
        "waves": 3,
    }
    assert [flow["flow_id"] for flow in payload["flows"]] == ["flow-1", "flow-2"]


def test_payload_without_flows_is_an_empty_board(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", lambda sid: [])

    payload = progress_payload("sess-none")

    assert payload["flows"] == []
    assert payload["totals"] == {"flows": 0, "total": 0, "done": 0, "current_wave": 0, "waves": 0}


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
    import json

    from agent.tools.taskflow import progress_push

    socket = _FakeWebSocket()
    monkeypatch.setattr(
        progress_push.relation_register, "get_websocket_by_session_id", lambda sid: socket
    )
    monkeypatch.setattr(
        progress_push.store_sqlite,
        "get_active_flows_sync",
        lambda sid: [_flow([_step("a", status="done")])],
    )

    await progress_push.push_taskflow_progress("sess-1")

    assert len(socket.sent) == 1
    frame = json.loads(socket.sent[0])
    assert frame["event"] == "taskflow_updated"
    assert frame["session_id"] == "sess-1"
    assert frame["content"]["totals"]["done"] == 1


@pytest.mark.asyncio
async def test_push_is_a_quiet_no_op_without_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    monkeypatch.setattr(
        progress_push.relation_register, "get_websocket_by_session_id", lambda sid: None
    )
    called: list[str] = []
    monkeypatch.setattr(
        progress_push.store_sqlite, "get_active_flows_sync", lambda sid: called.append(sid) or []
    )

    await progress_push.push_taskflow_progress("sess-1")
    await progress_push.push_taskflow_progress("   ")

    # No socket ⇒ never even read the store; a blank session is skipped outright.
    assert called == []


@pytest.mark.asyncio
async def test_push_swallows_a_failing_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.taskflow import progress_push

    class _Boom:
        async def send_text(self, text: str) -> None:
            raise RuntimeError("socket gone")

    monkeypatch.setattr(
        progress_push.relation_register, "get_websocket_by_session_id", lambda sid: _Boom()
    )
    monkeypatch.setattr(progress_push.store_sqlite, "get_active_flows_sync", lambda sid: [])

    # Must not raise: a send failure can never break the tool that mutated the flow.
    await progress_push.push_taskflow_progress("sess-1")


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
    import json

    from agent.tools.taskflow import progress_push

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
    monkeypatch.setattr(
        progress_push.relation_register, "get_websocket_by_session_id", lambda sid: socket
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
    assert frame["content"]["flows"][0]["waves"][1]["steps"][0]["step_id"] == "b"
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
