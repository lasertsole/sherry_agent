"""GAP-8: step-level retry policy.

A step may declare ``retry_policy = {max_retries, retry_delay_seconds,
retry_on}``. ``retry_count`` counts re-dispatches (the original dispatch is
free), so a retry is allowed while ``retry_count < max_retries``:

* ``taskflow_run_task`` stores the policy on the step (both the blocked and the
  dispatched DAG paths).
* ``taskflow_dispatch`` enforces the budget for a manual re-dispatch and
  increments ``retry_count`` when it spawns one.
* ``taskflow_wait_all`` turns a settled child of a policy step into an
  automatic re-dispatch while the budget lasts; on exhaustion it marks the
  step done and appends a failure-note result.
* ``taskflow_resume`` re-dispatches when the injected result text classifies
  as a failure allowed by ``retry_on`` (empty list = every classified failure);
  an already-injected child is never retried.

The dispatch seam (``_dispatch.dispatch_child``) and wait_all's registry seam
are monkeypatched in every test; the real spawn pipeline and subagent registry
are never touched. Policies in these tests use ``retry_delay_seconds: 0.0`` so
no test sleeps.
"""

import importlib
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools._shared import step_status

# The family package re-exports the tool under the same name, shadowing the
# submodule for package-attribute traversal; importlib resolves the real module
# from sys.modules so monkeypatching the registry seam still works.
wait_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_wait_all")

pytestmark = [pytest.mark.unit]

_SESSION = "sess-1"
_OWNER_KEY = "agent:main:session:sess-1"
_POLICY = {"max_retries": 2, "retry_delay_seconds": 0.0, "retry_on": []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DispatchRecorder:
    """Fake ``_dispatch.dispatch_child`` returning pre-seeded keys in order."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(
        self, task: str, requester_session_key: str, label: str | None = None
    ) -> str:
        self.calls.append((task, requester_session_key, label))
        if len(self.calls) > len(self.keys):
            raise AssertionError(
                f"unexpected spawn #{len(self.calls)} (only {len(self.keys)} key(s) seeded)"
            )
        return self.keys[len(self.calls) - 1]


class _Run:
    """Minimal subagent run record (only liveness matters here)."""

    def __init__(self, key: str, live: bool = True) -> None:
        self.child_session_key = key
        self.live = live


def _patch_registry(monkeypatch: pytest.MonkeyPatch, runs: dict[str, _Run] | None = None) -> None:
    """Registry seam where unknown keys are settled (dead) children."""
    runs_map = dict(runs or {})

    def fake_get_run(child_session_key: str) -> _Run | None:
        return runs_map.get(child_session_key)

    def fake_is_live(run: _Run) -> bool:
        return bool(run.live)

    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", fake_get_run)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", fake_is_live)


def _step(
    step_id: str,
    *,
    status: StepStatus | str = StepStatus.DISPATCHED,
    child: str | None = None,
    retry_policy: dict | None = None,
    retry_count: int | None = None,
) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "depends_on": [],
        "status": str(status),
    }
    if child is not None:
        step["child_session_key"] = child
    if retry_policy is not None:
        step["retry_policy"] = retry_policy
        step["retry_count"] = 0 if retry_count is None else retry_count
    elif retry_count is not None:
        step["retry_count"] = retry_count
    return step


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


async def _create(
    tools: dict, flow_id: str, steps: list[dict] | None = None, *, session_id: str = _SESSION
) -> None:
    out = await tools["taskflow_create"].coroutine(
        flow_id=flow_id,
        description="retry probe",
        initial_state={"steps": list(steps or [])},
        session_id=session_id,
    )
    assert "Error" not in out, out


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id)
    assert flow is not None
    return flow


def _step_of(flow: dict, step_id: str) -> dict:
    return next(s for s in flow["state"]["steps"] if s.get("step_id") == step_id)


async def _wait_all(flow_id: str) -> str:
    return await wait_mod.taskflow_wait_all.coroutine(
        flow_id, timeout_seconds=1.0, poll_interval_seconds=0.01
    )


# ---------------------------------------------------------------------------
# run_task: policy storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_policy_stored_on_step(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    # Given a flow and a fake dispatch seam
    fake = _DispatchRecorder("child-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(tools, "flow-1")

    # When a step is dispatched with a retry policy
    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="flaky step", retry_policy=_POLICY, session_id=_SESSION
    )

    # Then the policy is persisted on the step and the counter starts at 0
    assert "dispatched" in out, out
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step["retry_policy"] == _POLICY
    assert step["retry_count"] == 0
    assert step["child_session_key"] == "child-1"


@pytest.mark.asyncio
async def test_run_task_without_policy_stores_no_retry_policy(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow and a fake dispatch seam
    monkeypatch.setattr(_dispatch, "dispatch_child", _DispatchRecorder("child-1"))
    tools = _tools()
    await _create(tools, "flow-1")

    # When a step is dispatched without a retry policy
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="plain step", session_id=_SESSION
    )

    # Then no policy key is written (pre-GAP-8 shape preserved)
    step = _step_of(await _flow("flow-1"), "step-1")
    assert "retry_policy" not in step


@pytest.mark.asyncio
async def test_run_task_rejects_malformed_retry_policy(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow and a fake dispatch seam
    fake = _DispatchRecorder("child-1")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(tools, "flow-1")
    before = await _flow("flow-1")

    # When the policy is not a dict
    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="bad policy", retry_policy="nope", session_id=_SESSION
    )

    # Then it is rejected before any spawn or state change
    assert out.startswith("Error:"), out
    assert "retry_policy" in out
    assert fake.calls == []
    after = await _flow("flow-1")
    assert after["expected_revision"] == before["expected_revision"]
    assert after["state"]["steps"] == []


# ---------------------------------------------------------------------------
# wait_all: dead child auto re-dispatch / exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_count_increments_on_redispatch(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a dispatched policy step whose child has settled (dead)
    fake = _DispatchRecorder("child-1", "child-2")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    tools = _tools()
    await _create(tools, "flow-1")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="flaky step", retry_policy=_POLICY, session_id=_SESSION
    )

    # When wait_all detects the dead child
    out = await _wait_all("flow-1")

    # Then a replacement is spawned, the counter increments, and the step is
    # dispatched again on the new child key
    assert "re-dispatched" in out, out
    assert len(fake.calls) == 2
    assert fake.calls[1][1] == _OWNER_KEY  # retry uses the flow creator as requester
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step["retry_count"] == 1
    assert step["child_session_key"] == "child-2"
    assert step_status(step) == StepStatus.DISPATCHED


@pytest.mark.asyncio
async def test_retry_exhausted_no_redispatch(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    # Given a policy step after its 3rd child death (retry_count == max_retries)
    fake = _DispatchRecorder("child-next")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    tools = _tools()
    await _create(
        tools,
        "flow-1",
        [_step("step-1", child="child-dead", retry_policy=_POLICY, retry_count=2)],
    )

    # When wait_all detects the dead child
    out = await _wait_all("flow-1")

    # Then nothing is spawned, the step is marked done, and a failure note is
    # recorded in the flow results
    assert fake.calls == []
    assert "exhausted" in out.lower(), out
    flow = await _flow("flow-1")
    step = _step_of(flow, "step-1")
    assert step_status(step) == StepStatus.DONE
    assert step["retry_count"] == 2
    notes = [
        r for r in flow["state"]["results"] if "retry budget exhausted" in str(r.get("result"))
    ]
    assert len(notes) == 1
    assert notes[0]["child_session_key"] == "child-dead"


@pytest.mark.asyncio
async def test_no_retry_policy_no_redispatch(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    # Given a dispatched step WITHOUT a retry policy and a dead child
    fake = _DispatchRecorder("child-next")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    tools = _tools()
    await _create(tools, "flow-1", [_step("step-1", child="child-dead")])

    # When wait_all runs
    out = await _wait_all("flow-1")

    # Then it keeps the legacy settled report: no spawn, no retry bookkeeping,
    # step left dispatched for the orchestrator to resume
    assert fake.calls == []
    assert "settled=True" in out
    assert "retry" not in out.lower()
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step_status(step) == StepStatus.DISPATCHED


@pytest.mark.asyncio
async def test_wait_all_skips_child_with_injected_result(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a policy step whose child result was already injected
    fake = _DispatchRecorder("child-next")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    tools = _tools()
    step = _step("step-1", child="child-done", retry_policy=_POLICY, retry_count=0)
    await _create(tools, "flow-1", [step])
    await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="child-done", result="all good"
    )

    # When wait_all polls the now-done step
    out = await _wait_all("flow-1")

    # Then the injected child is never retried
    assert fake.calls == []
    assert "no dispatched" in out.lower() or "retry:" not in out


# ---------------------------------------------------------------------------
# resume: result-text failure classification
# ---------------------------------------------------------------------------


def _classify_policy() -> dict:
    return {"max_retries": 1, "retry_delay_seconds": 0.0, "retry_on": ["timeout"]}


@pytest.mark.asyncio
async def test_retry_on_filters_error_types(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    # Given two policy steps that only retry timeouts, and a fake seam
    fake = _DispatchRecorder("child-r1")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(
        tools, "flow-rate", [_step("step-1", child="child-orig", retry_policy=_classify_policy())]
    )
    await _create(
        tools,
        "flow-timeout",
        [_step("step-1", child="child-orig", retry_policy=_classify_policy())],
    )

    # When a rate-limit failure is injected
    out_rate = await tools["taskflow_resume"].coroutine(
        flow_id="flow-rate",
        child_session_key="child-orig",
        result="ERROR: rate limit exceeded (429)",
    )

    # Then it is filtered out: no spawn, the step is done
    assert "retry:" not in out_rate, out_rate
    assert fake.calls == []
    assert step_status(_step_of(await _flow("flow-rate"), "step-1")) == StepStatus.DONE

    # When a timeout failure is injected into the other flow
    out_timeout = await tools["taskflow_resume"].coroutine(
        flow_id="flow-timeout",
        child_session_key="child-orig",
        result="child aborted: operation timed out after 300s",
    )

    # Then it classifies as timeout and re-dispatches once
    assert "re-dispatched" in out_timeout, out_timeout
    assert len(fake.calls) == 1
    step = _step_of(await _flow("flow-timeout"), "step-1")
    assert step["retry_count"] == 1
    assert step["child_session_key"] == "child-r1"
    assert step_status(step) == StepStatus.DISPATCHED


@pytest.mark.asyncio
async def test_resume_success_result_marks_done_without_redispatch(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a policy step on a healthy child
    fake = _DispatchRecorder("child-next")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(tools, "flow-1", [_step("step-1", child="child-1", retry_policy=_POLICY)])

    # When a clean result is injected
    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="child-1", result="report written, all checks PASS"
    )

    # Then the step is done and nothing is re-dispatched
    assert "TaskFlow resumed" in out
    assert fake.calls == []
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step_status(step) == StepStatus.DONE
    assert step["retry_count"] == 0


@pytest.mark.asyncio
async def test_resume_misleading_success_output_does_not_retry(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Adversarial (misleading_success_output): the child claims success and/or
    # phrases cleanliness with a literal error word. Neither may be treated as
    # a failure, so no retry is burned and the step is done for the
    # orchestrator to judge.
    fake = _DispatchRecorder("child-next")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(tools, "flow-1", [_step("step-1", child="child-1", retry_policy=_POLICY)])
    await _create(tools, "flow-2", [_step("step-1", child="child-2", retry_policy=_POLICY)])

    out_brag = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1",
        child_session_key="child-1",
        result="SUCCESS! Everything is fine, trust me.",
    )
    out_clean = await tools["taskflow_resume"].coroutine(
        flow_id="flow-2",
        child_session_key="child-2",
        result="All checks passed: no errors detected, error-free run.",
    )

    assert "retry:" not in out_brag
    assert "retry:" not in out_clean
    assert fake.calls == []
    for flow_id in ("flow-1", "flow-2"):
        assert step_status(_step_of(await _flow(flow_id), "step-1")) == StepStatus.DONE


@pytest.mark.asyncio
async def test_resume_retry_then_wait_all_exhausts_budget(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a policy step (max_retries=1) dispatched through run_task
    fake = _DispatchRecorder("child-1", "child-2")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_registry(monkeypatch)
    tools = _tools()
    await _create(tools, "flow-1")
    policy = {"max_retries": 1, "retry_delay_seconds": 0.0, "retry_on": []}
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="flaky step", retry_policy=policy, session_id=_SESSION
    )

    # When the first failure is injected, resume re-dispatches (retry_count 0 -> 1)
    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="child-1", result="ERROR: upstream failed"
    )
    assert "re-dispatched" in out, out
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step["retry_count"] == 1
    assert step["child_session_key"] == "child-2"

    # When the replacement child also dies and wait_all runs out of budget
    out = await _wait_all("flow-1")

    # Then no third spawn happens; the step is done with a failure note
    assert len(fake.calls) == 2
    assert "exhausted" in out.lower(), out
    flow = await _flow("flow-1")
    step = _step_of(flow, "step-1")
    assert step_status(step) == StepStatus.DONE
    assert step["retry_count"] == 1
    assert any("retry budget exhausted" in str(r.get("result")) for r in flow["state"]["results"])


# ---------------------------------------------------------------------------
# dispatch: manual re-dispatch budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_manual_redispatch_increments_retry_count(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a ready step that was already dispatched once (old child key)
    fake = _DispatchRecorder("child-new")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(
        tools,
        "flow-1",
        [
            _step(
                "step-1",
                status=StepStatus.READY,
                child="child-old",
                retry_policy=_POLICY,
                retry_count=0,
            )
        ],
    )

    # When it is manually re-dispatched
    out = await tools["taskflow_dispatch"].coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )

    # Then the spawn consumes one retry
    assert "dispatched" in out, out
    assert len(fake.calls) == 1
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step["retry_count"] == 1
    assert step["child_session_key"] == "child-new"


@pytest.mark.asyncio
async def test_dispatch_manual_redispatch_budget_exhausted(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a ready step whose retry budget is already used up
    fake = _DispatchRecorder("child-new")
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    tools = _tools()
    await _create(
        tools,
        "flow-1",
        [
            _step(
                "step-1",
                status=StepStatus.READY,
                child="child-old",
                retry_policy=_POLICY,
                retry_count=2,
            )
        ],
    )

    # When dispatch is attempted
    out = await tools["taskflow_dispatch"].coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )

    # Then the budget is enforced before any spawn
    assert out.startswith("Error:"), out
    assert "exhausted" in out.lower()
    assert fake.calls == []
    step = _step_of(await _flow("flow-1"), "step-1")
    assert step_status(step) == StepStatus.READY
