"""The ``aggregate_deps`` prompt is bounded per dependency and per block.

Measured risk (2026-11-25 plan): a stored step result can reach
``helpers_frozen_result_cap_bytes`` (24 kB), and ``build_task_with_dep_results``
appended every dependency's FULL result. A synthesis step with five dependencies
therefore injected ~120 k chars of upstream text into a subagent prompt, anew on
every dispatch/retry (it is re-derived from the stable ``results``).

The budget is deterministic water-filling: equal share first, leftover to the
still-clipped dependencies in ``depends_on`` order, never past the per-dep cap.
A result that fits is emitted byte-identically to the legacy format.
"""

from __future__ import annotations

import re

import pytest

from agent.tools.taskflow.step_judge import _clip_for_judge
from agent.tools.taskflow.tools._shared import build_task_with_dep_results
from config.features import STEP_JUDGE, TASKFLOW_INFRA

pytestmark = [pytest.mark.unit]

_PER_DEP = int(TASKFLOW_INFRA["aggregate_result_per_dep_max_chars"])
_TOTAL = int(TASKFLOW_INFRA["aggregate_result_total_max_chars"])
_SECTION = re.compile(r"### ([^\n]+)\n(.*?)(?=\n\n### |\Z)", re.DOTALL)


def _step(step_id: str, *, deps: list[str] | None = None, aggregate: bool = True) -> dict:
    return {
        "step_id": step_id,
        "task": f"do {step_id}",
        "depends_on": deps or [],
        "aggregate_deps": aggregate,
        "child_session_key": f"agent:main:subagent:{step_id}",
    }


def _result(step_id: str, text: str) -> dict:
    return {"child_session_key": f"agent:main:subagent:{step_id}", "result": text}


def _render(steps: list[dict], results: list[dict], deps: list[str]) -> str:
    synthesis = _step("synthesis", deps=deps)
    return build_task_with_dep_results(synthesis, [*steps, synthesis], results)


def _bodies(text: str) -> dict[str, str]:
    block = text.split("## Upstream Results\n", 1)[1]
    return {match.group(1): match.group(2) for match in _SECTION.finditer(block)}


def test_two_24k_dependencies_are_capped_and_marked() -> None:
    steps = [_step("dep-a"), _step("dep-b")]
    results = [_result("dep-a", "A" * 24_000), _result("dep-b", "B" * 24_000)]

    text = _render(steps, results, ["dep-a", "dep-b"])
    bodies = _bodies(text)

    assert list(bodies) == ["dep-a", "dep-b"]
    assert all(len(body) <= _PER_DEP for body in bodies.values())
    assert sum(len(body) for body in bodies.values()) <= _TOTAL
    assert all(
        "chars omitted; the full result stays in the flow's results" in b for b in bodies.values()
    )
    # Head+tail: the conclusion at the END survives the clip.
    assert bodies["dep-a"].endswith("A" * 10)
    assert bodies["dep-b"].endswith("B" * 10)


def test_short_dependencies_are_byte_identical_to_the_legacy_block() -> None:
    steps = [_step("dep-a"), _step("dep-b")]
    results = [_result("dep-a", "short A"), _result("dep-b", "short B")]

    text = _render(steps, results, ["dep-a", "dep-b"])

    expected = "do synthesis\n\n## Upstream Results\n### dep-a\nshort A\n\n### dep-b\nshort B"
    assert text == expected


def test_water_filling_keeps_the_short_result_whole() -> None:
    steps = [_step("dep-a"), _step("dep-b")]
    results = [_result("dep-a", "A" * 24_000), _result("dep-b", "brief finding")]

    bodies = _bodies(_render(steps, results, ["dep-a", "dep-b"]))

    assert bodies["dep-b"] == "brief finding"
    assert len(bodies["dep-a"]) <= _PER_DEP
    assert sum(len(body) for body in bodies.values()) <= _TOTAL


def test_five_10k_dependencies_fit_the_block_budget() -> None:
    ids = [f"dep-{index}" for index in range(5)]
    steps = [_step(dep_id) for dep_id in ids]
    results = [_result(dep_id, dep_id[-1] * 10_000) for dep_id in ids]

    bodies = _bodies(_render(steps, results, ids))

    assert list(bodies) == ids
    assert sum(len(body) for body in bodies.values()) <= _TOTAL
    assert all("omitted" in body for body in bodies.values())


def test_a_missing_result_keeps_the_legacy_placeholder() -> None:
    steps = [_step("dep-a")]
    results: list[dict] = []

    bodies = _bodies(_render(steps, results, ["dep-a"]))

    assert bodies["dep-a"] == "no result recorded"


def test_without_aggregate_deps_the_task_is_returned_unchanged() -> None:
    steps = [_step("dep-a")]
    results = [_result("dep-a", "A" * 24_000)]
    synthesis = _step("synthesis", deps=["dep-a"], aggregate=False)

    assert build_task_with_dep_results(synthesis, [*steps, synthesis], results) == "do synthesis"


def test_the_judge_clip_is_still_bounded_and_keeps_the_tail() -> None:
    text = "HEAD\n" + ("filler\n" * 5000) + "FINAL"

    clipped = _clip_for_judge(text)

    assert len(clipped) == int(STEP_JUDGE["max_result_chars"])
    assert clipped.startswith("HEAD")
    assert clipped.endswith("FINAL")
    assert "middle omitted for the judge" in clipped


def test_many_short_dependencies_are_not_collapsed() -> None:
    """The collapse is a FIT fallback, not a capacity one (100 short results fit)."""
    ids = [f"d{index}" for index in range(100)]
    steps = [_step(dep_id) for dep_id in ids]
    results = [_result(dep_id, "ten chars!") for dep_id in ids]

    bodies = _bodies(_render(steps, results, ids))

    assert list(bodies) == ids
    assert all(body == "ten chars!" for body in bodies.values())
    assert all("omitted" not in body for body in bodies.values())
