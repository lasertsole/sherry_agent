"""DAG waves: how a flow's steps group into parallelisable rounds.

A *wave* is one longest-path level of the dependency graph: every step in wave N
depends only on steps in earlier waves, so a wave is exactly the set of steps
that could run once the previous wave finished. TaskFlow schedules steps
individually (a step turns ``ready`` when its OWN dependencies are done), so waves
are a REPORTING view — what the progress panel shows — never a scheduling input.

Pure functions over the persisted step dicts: no store, no session, no I/O, so the
client-facing shape is unit-testable and the scheduler cannot grow a dependency on
it by accident.

Edge rules:

* a dependency id that is not in the list contributes nothing (a dep outside the
  step set can never be waited on);
* a dependency CYCLE (only reachable by editing ``state_json`` by hand —
  ``validate_steps_list`` rejects unknown ids, and the unlock pass is single-shot)
  is reported in a trailing wave flagged ``cyclic`` instead of looping or
  silently dropping the steps.
"""

from __future__ import annotations

from typing import Any

__all__ = ["STATUS_ORDER", "compute_waves", "steps_of"]

#: Statuses the wire shape always carries (zero-filled), in display order.
STATUS_ORDER: tuple[str, ...] = (
    "done",
    "dispatched",
    "ready",
    "blocked",
    "failed",
    "skipped",
    "cancelled",
)

#: Task text longer than this is clipped on the wire (the panel is an overview).
_TASK_WIDTH = 200


def steps_of(flow: dict[str, Any]) -> list[dict]:
    """The step list inside a flow row (``state.steps``, defensive)."""
    state = flow.get("state") or {}
    steps = state.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _status_counts(steps: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    for step in steps:
        status = str(step.get("status") or "ready")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _wire_step(step: dict) -> dict[str, str]:
    return {
        "step_id": str(step.get("step_id") or ""),
        "task": str(step.get("task") or "")[:_TASK_WIDTH],
        "status": str(step.get("status") or "ready"),
    }


def compute_waves(steps: list[dict]) -> list[dict]:
    """Group ``steps`` into waves ordered by dependency depth.

    Each wave is ``{"index" (1-based), "total", "done", "by_status", "steps"}``,
    plus ``"cyclic": True`` on the trailing wave that holds unresolvable steps.
    Step order inside a wave is the input order (the plan's own ordering).
    """
    ordered = [step for step in steps if str(step.get("step_id") or "")]
    known = {str(step["step_id"]) for step in ordered}
    deps: dict[str, list[str]] = {
        str(step["step_id"]): [
            str(dep) for dep in (step.get("depends_on") or []) if str(dep) in known
        ]
        for step in ordered
    }

    level: dict[str, int] = {}
    on_stack: set[str] = set()
    cyclic: set[str] = set()

    def resolve(step_id: str) -> int:
        if step_id in level:
            return level[step_id]
        if step_id in on_stack:  # back edge: the cycle's steps are reported apart
            cyclic.add(step_id)
            return 0
        on_stack.add(step_id)
        depth = 0
        for dep in deps.get(step_id, []):
            if dep in on_stack:
                cyclic.add(dep)
                cyclic.add(step_id)
                continue
            depth = max(depth, resolve(dep) + 1)
        on_stack.discard(step_id)
        level[step_id] = depth
        return depth

    for step in ordered:
        resolve(str(step["step_id"]))

    buckets: dict[int, list[dict]] = {}
    for step in ordered:
        step_id = str(step["step_id"])
        if step_id in cyclic:
            continue
        buckets.setdefault(level[step_id], []).append(step)

    waves: list[dict] = []
    for index, depth in enumerate(sorted(buckets), start=1):
        bucket = buckets[depth]
        counts = _status_counts(bucket)
        waves.append(
            {
                "index": index,
                "total": len(bucket),
                "done": counts["done"],
                "by_status": counts,
                "steps": [_wire_step(step) for step in bucket],
            }
        )

    if cyclic:
        bucket = [step for step in ordered if str(step["step_id"]) in cyclic]
        counts = _status_counts(bucket)
        waves.append(
            {
                "index": len(waves) + 1,
                "total": len(bucket),
                "done": counts["done"],
                "by_status": counts,
                "steps": [_wire_step(step) for step in bucket],
                "cyclic": True,
            }
        )
    return waves
