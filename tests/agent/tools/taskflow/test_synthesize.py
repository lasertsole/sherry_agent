"""Dependency-result aggregation helper (taskflow synthesize increment).

Phase 2's only new capability is synthesize: a step that opts in via
``aggregate_deps`` gets its dependency steps' recorded results appended to the
task text at dispatch time. ``_shared.build_task_with_dep_results`` is that
pure function; the tool wiring is pinned separately in
test_synthesize_e2e.py.

Contracts covered:
(a) ``aggregate_deps`` falsy / no dependencies -> the original task text
    unchanged (the default opt-out path stays byte-identical).
(b) results are matched by each dependency's ``child_session_key`` against the
    resume-shaped records ``{child_session_key, result, result_hash}`` and
    appended in ``depends_on`` order.
(c) a dependency with no recorded result (unknown step id, never spawned, or
    absent from the ledger) contributes a "no result recorded" placeholder and
    never raises.
"""

import pytest

from agent.tools.taskflow.tools._shared import build_task_with_dep_results

pytestmark = [pytest.mark.unit]

_HEADER = "## Upstream Results"
_PLACEHOLDER = "no result recorded"


def _dep_step(step_id: str, *, child: str | None = None, status: str = "done") -> dict:
    step = {"step_id": step_id, "task": f"task-{step_id}", "depends_on": [], "status": status}
    if child is not None:
        step["child_session_key"] = child
    return step


def _record(child: str, result: str) -> dict:
    """A result ledger entry in the exact shape taskflow_resume records."""
    return {"child_session_key": child, "result": result, "result_hash": f"hash::{child}"}


def _synthesis(*depends_on: str, aggregate: bool = True) -> dict:
    return {
        "step_id": "step-9",
        "task": "Synthesize the findings.",
        "depends_on": list(depends_on),
        "status": "ready",
        "aggregate_deps": aggregate,
    }


# ---------------------------------------------------------------------------
# (a) opt-out / nothing to aggregate -> original task
# ---------------------------------------------------------------------------


def test_aggregate_flag_falsy_returns_original_task():
    # Given a step that did NOT opt in, with a dependency result available
    step = _synthesis("step-1", aggregate=False)
    steps = [_dep_step("step-1", child="child-1")]
    results = [_record("child-1", "FINDINGS-XYZ")]

    # When the dispatch task text is built
    text = build_task_with_dep_results(step, steps, results)

    # Then the task is untouched: no aggregation section leaks in
    assert text == "Synthesize the findings."
    assert _HEADER not in text
    assert "FINDINGS-XYZ" not in text


def test_no_dependencies_returns_original_task():
    # Given an opted-in step that declares no dependencies
    step = _synthesis()

    # When the dispatch task text is built
    text = build_task_with_dep_results(step, [], [])

    # Then there is nothing to aggregate and the task is unchanged
    assert text == "Synthesize the findings."
    assert _HEADER not in text


# ---------------------------------------------------------------------------
# (b) opt-in: results matched by dependency child_session_key
# ---------------------------------------------------------------------------


def test_results_matched_by_dependency_child_session_key():
    # Given two done dependencies whose results are recorded by child key
    step = _synthesis("step-1", "step-2")
    steps = [_dep_step("step-1", child="child-1"), _dep_step("step-2", child="child-2")]
    results = [_record("child-2", "SECOND-RESULT"), _record("child-1", "FIRST-RESULT")]

    # When the dispatch task text is built
    text = build_task_with_dep_results(step, steps, results)

    # Then both results are appended under the header, ordered by depends_on
    assert text.startswith("Synthesize the findings.")
    assert _HEADER in text
    assert "FIRST-RESULT" in text
    assert "SECOND-RESULT" in text
    assert text.index("FIRST-RESULT") < text.index("SECOND-RESULT")


def test_resume_shaped_record_is_consumed():
    # Given a resume-shaped record carrying all three documented fields
    step = _synthesis("step-1")
    steps = [_dep_step("step-1", child="child-1")]
    record = _record("child-1", "FINDINGS-XYZ")
    assert set(record) == {"child_session_key", "result", "result_hash"}

    # When the aggregation runs
    text = build_task_with_dep_results(step, steps, [record])

    # Then the recorded result body is what lands in the task
    assert "FINDINGS-XYZ" in text
    assert "hash::child-1" not in text


# ---------------------------------------------------------------------------
# (c) degradation: missing step / missing result -> placeholder, never raise
# ---------------------------------------------------------------------------


def test_dependency_without_recorded_result_gets_placeholder():
    # Given a done dependency whose result was never recorded
    step = _synthesis("step-1")
    steps = [_dep_step("step-1", child="child-1")]

    # When the aggregation runs with an empty results ledger
    text = build_task_with_dep_results(step, steps, [])

    # Then a placeholder is emitted instead of raising
    assert _PLACEHOLDER in text
    assert "step-1" in text


def test_unknown_dependency_id_gets_placeholder():
    # Given a dependency id that is not present in the step list
    step = _synthesis("ghost")

    # When the aggregation runs
    text = build_task_with_dep_results(step, [], [])

    # Then it degrades to a placeholder rather than raising
    assert _PLACEHOLDER in text
    assert "ghost" in text


def test_dependency_done_without_child_key_gets_placeholder():
    # Given a dependency that is done but never spawned a child
    step = _synthesis("step-1")
    steps = [_dep_step("step-1")]

    # When the aggregation runs
    text = build_task_with_dep_results(step, steps, [])

    # Then the missing child key degrades to the placeholder
    assert _PLACEHOLDER in text
    assert "step-1" in text
