"""Quality eval suite for the TaskFlow long-running-task engine.

Drives the real orchestration loop end to end — create → run_task → dispatch →
wait_all → resume — over a 3-step dependent DAG whose children are real LLM
subagents (sandboxed tools, deterministic micro-tasks), then scores step
success, flow completion and wall time.

Usage:
    uv run python evals/evals.py long_running_task
"""

from __future__ import annotations

import asyncio
import csv
import faulthandler
import json
import re
import sys
import time
import uuid
import types
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.sandbox import EvalSandbox  # noqa: E402

_FLOW = "flow-evals-lrt"
_TIMEOUT_S = 240.0
_WAIT_TIMEOUT_S = 600.0
_POLL_INTERVAL_S = 5.0
_MAX_ROUNDS = 8

# A→B→C chain; each step replies with a verifiable token.
STEPS: list[dict[str, Any]] = [
    {
        "label": "step-a",
        "task": "Reply with exactly the token ORCHID-7 and nothing else.",
        "expected": "orchid-7",
    },
    {
        "label": "step-b",
        "task": "Reply with exactly the token FERN-12 and nothing else.",
        "expected": "fern-12",
    },
    {
        "label": "step-c",
        "task": "Reply with exactly the token BASALT-3 and nothing else.",
        "expected": "basalt-3",
    },
]
for _index, _step in enumerate(STEPS, start=1):
    _step["step_id"] = f"step-{_index}"
    _step["deps"] = [] if _index == 1 else [f"step-{_index - 1}"]


def _extract(text: str, key: str) -> str:
    match = re.search(rf"{key}=([^\s,]+)", text)
    return match.group(1) if match else ""


def _tool_map() -> dict:
    """Name-indexed taskflow tools, mirroring the project test's _tool_map()."""
    from agent.tools.taskflow import build_taskflow_tools

    return {tool.name: tool for tool in build_taskflow_tools()}


async def _wait_children_settled(child_keys: list[str], timeout_s: float) -> set[str]:
    """Poll the registry directly for TERMINAL state; return keys still unsettled."""
    from agent.tools.subagent.registry import get_run_by_child_session_key
    from agent.tools.subagent.types.registry import ExecutionStatus

    deadline = time.monotonic() + timeout_s
    pending = set(child_keys)
    while pending:
        still = []
        for key in sorted(pending):
            run = get_run_by_child_session_key(key)
            if run is None or run.execution.status == ExecutionStatus.TERMINAL:
                pending.discard(key)
                continue
            still.append(f"{key[:8]}={run.execution.status.value}")
        if not still:
            return set()
        if time.monotonic() >= deadline:
            return pending
        print(f"  [wait] pending: {still}", flush=True)
        await asyncio.sleep(_POLL_INTERVAL_S)
    return pending


async def _run_flow(session_id: str) -> list[dict[str, object]]:
    from agent.tools.subagent.registry import get_run_by_child_session_key
    from agent.tools.taskflow.registry import store_sqlite

    # Direct .coroutine invocation is the project's canonical way to drive the
    # real tool functions (mirrors tests/agent/tools/taskflow/test_taskflow_tools.py).
    tools = _tool_map()

    created = await tools["taskflow_create"].coroutine(
        flow_id=_FLOW, description="evals DAG chain", session_id=session_id
    )
    if created.startswith("Error:"):
        raise RuntimeError(f"taskflow_create failed: {created}")

    booked: dict[str, dict[str, Any]] = {}
    for step in STEPS:
        dispatched_at = time.monotonic()
        registered = await tools["taskflow_run_task"].coroutine(
            flow_id=_FLOW,
            task=str(step["task"]),
            label=str(step["label"]),
            depends_on=list(step["deps"]),
            session_id=session_id,
        )
        if registered.startswith("Error:"):
            raise RuntimeError(f"run_task({step['label']}) failed: {registered}")
        status = "blocked" if "(blocked)" in registered else "dispatched"
        booked[str(step["step_id"])] = {
            "label": step["label"],
            "status": status,
            "child_session_key": _extract(registered, "child_session_key"),
            "dispatched_at": dispatched_at if status == "dispatched" else None,
        }

    samples: dict[str, dict[str, object]] = {}
    resumed: set[str] = set()
    for _round in range(_MAX_ROUNDS):
        for step in STEPS:
            step_id = str(step["step_id"])
            info = booked[step_id]
            if info["status"] != "blocked":
                continue
            if not all(dep in resumed for dep in list(step["deps"])):
                continue
            info["dispatched_at"] = time.monotonic()
            dispatched = await tools["taskflow_dispatch"].coroutine(
                flow_id=_FLOW, step_ids=[step_id], session_id=session_id
            )
            if dispatched.startswith("Error:"):
                raise RuntimeError(f"dispatch({step_id}) failed: {dispatched}")
            info["status"] = "dispatched"
            # taskflow_dispatch's return carries no child key — read it from
            # the flow state, where dispatch persisted it on the step.
            flow = store_sqlite.get_flow_sync(_FLOW, session_id)
            flow_steps = {
                str(s["step_id"]): s for s in (flow or {}).get("state", {}).get("steps", [])
            }
            info["child_session_key"] = str(
                flow_steps.get(step_id, {}).get("child_session_key", "")
            )

        if all(booked[str(s["step_id"])]["status"] == "done" for s in STEPS):
            break

        dispatched_keys = [
            str(booked[str(s["step_id"])]["child_session_key"])
            for s in STEPS
            if booked[str(s["step_id"])]["status"] == "dispatched"
        ]
        unsettled = await _wait_children_settled(dispatched_keys, _WAIT_TIMEOUT_S)
        for step in STEPS:
            step_id = str(step["step_id"])
            info = booked[step_id]
            child_key = str(info["child_session_key"])
            if child_key in unsettled:
                samples[step_id] = {
                    "step_id": step_id,
                    "label": step["label"],
                    "success": False,
                    "outcome": "wait-timeout",
                    "child_session_key": child_key,
                    "latency_s": round(time.monotonic() - float(info["dispatched_at"]), 2),
                }
                print(f"  [{step['label']}] success=False (wait-timeout)", flush=True)
                resumed.add(step_id)
                info["status"] = "done"
                await tools["taskflow_resume"].coroutine(
                    flow_id=_FLOW,
                    child_session_key=child_key,
                    result="[evals] child did not settle within the wait budget",
                    session_id=session_id,
                )

        for step in STEPS:
            step_id = str(step["step_id"])
            info = booked[step_id]
            if info["status"] != "dispatched":
                continue
            child_key = str(info["child_session_key"])
            run = get_run_by_child_session_key(child_key)
            if run is None or run.execution.outcome is None:
                continue
            result_text = (run.completion.result_text or "").strip()
            expected = str(step["expected"])
            success = (
                run.execution.outcome.status.value == "ok"
                and expected.lower() in result_text.lower()
            )
            latency = round(time.monotonic() - float(info["dispatched_at"]), 2)
            samples[step_id] = {
                "step_id": step_id,
                "label": step["label"],
                "success": success,
                "outcome": run.execution.outcome.status.value,
                "child_session_key": child_key,
                "latency_s": latency,
            }
            print(f"  [{step['label']}] success={success} ({latency}s)", flush=True)
            resumed.add(step_id)
            info["status"] = "done"
            await tools["taskflow_resume"].coroutine(
                flow_id=_FLOW,
                child_session_key=child_key,
                result=result_text[:4000],
                session_id=session_id,
            )

    if len(resumed) != len(STEPS):
        raise RuntimeError(f"flow stalled: resumed={sorted(resumed)} of {len(STEPS)}")
    progress = await tools["taskflow_progress"].coroutine(flow_id=_FLOW, session_id=session_id)
    first_line = progress.splitlines()[0] if progress else "(empty)"
    print(f"  [progress] {first_line}", flush=True)
    return [samples[str(step["step_id"])] for step in STEPS]


def _install_sandboxed_dispatch() -> tuple[types.ModuleType, object]:
    """Replace the taskflow dispatch seam with a keep-records spawn wrapper.

    Production ``dispatch_child`` spawns with cleanup="delete": once the
    announce flow finishes, the run record is removed from the registry and a
    post-settle result lookup returns None. The eval must read each child's
    result AFTER settle, so it spawns with cleanup="keep" and
    expects_completion_message=False (fire-and-forget config — announce
    delivery is the subagent suite's concern, not this one).

    Returns (module, original_dispatch) for restoration.
    """
    import agent.tools.taskflow.tools._dispatch as dispatch_module

    original = dispatch_module.dispatch_child

    async def sandboxed_dispatch(
        task: str, requester_session_key: str, label: str | None = None
    ) -> str:
        from agent.tools.subagent.spawn.core import spawn_subagent_direct

        result = await spawn_subagent_direct(
            task=task,
            requester_session_key=requester_session_key,
            label=label,
            cleanup="keep",
            expects_completion_message=False,
            run_timeout_seconds=_TIMEOUT_S,
        )
        if result.status != "accepted" or not result.child_session_key:
            raise RuntimeError(f"status={result.status} error={result.error}")
        return result.child_session_key

    dispatch_module.dispatch_child = sandboxed_dispatch
    return dispatch_module, original


def main() -> None:
    """Run the TaskFlow DAG eval under the sandbox and write the report."""
    # If the run wedges, dump every thread's stack and exit instead of hanging.
    faulthandler.dump_traceback_later(180, exit=True)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "long_running_task" / run_id
    results_dir.mkdir(parents=True)

    # taskflow's requester_session_key() prepends "agent:main:session:" itself,
    # so session_id must be the raw id.
    session_id = f"evals-lrt-{uuid.uuid4().hex[:8]}"
    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    dispatch_module, original_dispatch = _install_sandboxed_dispatch()
    started = time.monotonic()
    try:
        samples = asyncio.run(_run_flow(session_id))
    except Exception as exc:  # noqa: BLE001 — eval records the failure and exits cleanly
        print(f"[evals] long_running_task FAILED: {exc}", flush=True)
        sys.stdout.flush()
        raise SystemExit(1) from exc
    finally:
        setattr(dispatch_module, "dispatch_child", original_dispatch)
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    successes = sum(1 for s in samples if s["success"])
    aggregate = {
        "steps_total": len(STEPS),
        "steps_success": successes,
        "step_success_rate": round(successes / len(STEPS), 4),
        "total_seconds": round(time.monotonic() - started, 2),
    }

    report = {"run_id": run_id, "aggregate": aggregate, "steps": samples}
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (results_dir / "scores.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["step_id", "label", "success", "outcome", "child_session_key", "latency_s"],
        )
        writer.writeheader()
        for sample in samples:
            writer.writerow({k: sample[k] for k in writer.fieldnames})

    print(f"[evals] long_running_task aggregate: {aggregate}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)

    sys.stdout.flush()


if __name__ == "__main__":
    main()
