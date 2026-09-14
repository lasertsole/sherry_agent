"""Quality eval suite for the subagent spawn pipeline.

Runs a bench of self-contained, deterministic tasks through the real
``spawn_subagent_direct`` pipeline (real LLM children, sandboxed tools) and
scores task success + latency.

Usage:
    uv run python evals/evals.py subagent
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.sandbox import EvalSandbox  # noqa: E402

_TIMEOUT_S = 240.0
_POLL_INTERVAL_S = 2.0

BENCH: list[tuple[str, str, str]] = [
    (
        "echo-token",
        "Reply with exactly the token PINEAPPLE-42 and nothing else.",
        "pineapple-42",
    ),
    (
        "arithmetic",
        "Compute 17 * 23. Reply with only the numeric result and nothing else.",
        "391",
    ),
    (
        "rate",
        "A train travels 360 km in 4 hours at constant speed. What is its average "
        "speed in km/h? Reply with only the number and nothing else.",
        "90",
    ),
]


async def _poll_terminal(run_id: str, timeout_s: float):
    """Poll the registry until the run reaches TERMINAL; raise TimeoutError."""
    from agent.tools.subagent.registry import get_run
    from agent.tools.subagent.types.registry import ExecutionStatus

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = get_run(run_id)
        if run is not None and run.execution.status == ExecutionStatus.TERMINAL:
            return run
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"run {run_id} not terminal within {timeout_s}s")


async def _run_bench() -> tuple[list[dict[str, object]], list[float]]:
    from agent.tools.subagent.spawn.core import spawn_subagent_direct
    from agent.tools.subagent.types.spawn import ContextMode, SpawnMode

    samples: list[dict[str, object]] = []
    latencies: list[float] = []
    for case, task, expected in BENCH:
        started = time.monotonic()
        outcome = "spawn-error"
        result_text = ""
        try:
            spawn = await spawn_subagent_direct(
                task=task,
                requester_session_key=f"agent:main:session:evals-sub-{uuid.uuid4().hex[:8]}",
                agent_id="main",
                spawn_mode=SpawnMode.RUN,
                cleanup="delete",
                context=ContextMode.ISOLATED,
                run_timeout_seconds=_TIMEOUT_S,
            )
            if spawn.status != "accepted" or spawn.run_id is None:
                outcome = f"spawn-{spawn.status}"
            else:
                run = await _poll_terminal(spawn.run_id, _TIMEOUT_S)
                outcome = (
                    run.execution.outcome.status.value
                    if run.execution.outcome is not None
                    else "no-outcome"
                )
                result_text = (run.completion.result_text or "").strip()
        except TimeoutError:
            outcome = "eval-timeout"
        except Exception as exc:  # noqa: BLE001 — eval records any pipeline failure
            outcome = f"exception: {type(exc).__name__}"

        success = outcome == "ok" and expected.lower() in result_text.lower()
        latency = round(time.monotonic() - started, 2)
        samples.append(
            {
                "case": case,
                "success": success,
                "outcome": outcome,
                "expected": expected,
                "result_text": result_text[:2000],
                "latency_s": latency,
            }
        )
        latencies.append(latency)
        print(f"  [{case}] success={success} outcome={outcome} ({latency}s)", flush=True)
    return samples, latencies


def main() -> None:
    """Run the subagent bench under the eval sandbox and write the report."""
    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "subagent" / run_id
    results_dir.mkdir(parents=True)

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    try:
        samples, latencies = asyncio.run(_run_bench())
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    successes = sum(1 for s in samples if s["success"])
    aggregate = {
        "success_rate": round(successes / len(samples), 4) if samples else 0.0,
        "mean_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "median_latency_s": sorted(latencies)[len(latencies) // 2] if latencies else 0.0,
    }

    report = {"run_id": run_id, "aggregate": aggregate, "samples": samples}
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (results_dir / "scores.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["case", "success", "outcome", "expected", "latency_s"]
        )
        writer.writeheader()
        for sample in samples:
            writer.writerow({k: sample[k] for k in writer.fieldnames})

    print(f"[evals] subagent aggregate: {aggregate}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
