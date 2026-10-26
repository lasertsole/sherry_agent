"""Which model seam each internal call site uses (T2 / C1 / C3-C5).

A source-level pin is deliberate: the whole point of the two optional families
is that they own a NAMED set of call sites, and a future refactor that switches
one back (or leaks TaskFlow's model into the completion judge) would otherwise
pass every behavioural test — the fallback makes both seams look identical.
"""

from pathlib import Path

import pytest

# unit marker is REQUIRED for gate visibility: the split runner selects groups
# by marker expression, so an unmarked file is invisible to every CI group
# even though a plain `pytest tests/` collects it.
pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[4]

TASKFLOW_CALL_SITES = (
    "agent/tools/taskflow/tools/taskflow_plan.py",
    "agent/tools/taskflow/tools/taskflow_replan.py",
    "agent/tools/taskflow/tools/taskflow_finish.py",
    "agent/tools/taskflow/step_judge.py",
)

NON_TASKFLOW_CALL_SITES = (
    "agent/tools/subagent/spawn/completion_judge.py",
    "agent/middlewares/summarization/nudges.py",
    "agent/tools/subagent/spawn/core.py",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", TASKFLOW_CALL_SITES)
def test_taskflow_call_sites_use_the_taskflow_seam(path):
    source = _text(path)

    assert "build_taskflow_llm" in source
    assert "build_auxiliary_llm" not in source


@pytest.mark.parametrize("path", NON_TASKFLOW_CALL_SITES)
def test_non_taskflow_call_sites_stay_on_auxiliary(path):
    source = _text(path)

    assert "build_auxiliary_llm" in source
    assert "build_taskflow_llm" not in source


def test_agent_core_builds_both_auxiliary_and_compression_models():
    source = _text("agent/core.py")

    assert "build_auxiliary_llm" in source  # C3: still built
    assert "build_compression_llm" in source
