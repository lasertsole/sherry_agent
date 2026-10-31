"""Every GitHub Actions workflow must be PARSEABLE YAML with real jobs.

GitHub turns an unparsable workflow into a run with ZERO jobs and a red X: the
push "runs" the workflow file, fails instantly, and nothing else is reported —
the failure mode that bit `ci.yml` on 2026-10-30, when a step name was edited to
``- name: Check docs carry their own language (en: no CJK, ja/ko: …)`` and the
unquoted ``: `` inside the scalar broke the whole file (a colon+space opens a
mapping, even in a plain scalar).

These tests make that mistake fail locally: a workflow that does not parse, or
parses without jobs / without a trigger, is red in the normal pytest gate.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = sorted(glob.glob(str(_ROOT / ".github" / "workflows" / "*.y*ml")))


def _workflow_paths() -> list[Path]:
    return [Path(path) for path in _WORKFLOWS]


def test_the_workflow_directory_is_not_empty() -> None:
    assert _workflow_paths(), "no workflow files found — the glob is wrong, not the repo"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda path: path.name)
def test_workflow_parses_with_jobs_and_a_trigger(path: Path) -> None:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
    # PyYAML reads the `on:` key as boolean True (YAML 1.1), so accept both.
    assert "on" in data or True in data, f"{path.name} declares no trigger"
    jobs = data.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} declares no jobs"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda path: path.name)
def test_every_job_has_a_runner_or_reuses_a_workflow(path: Path) -> None:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    for name, job in data["jobs"].items():
        assert isinstance(job, dict), f"{path.name}:{name} is not a mapping"
        assert "runs-on" in job or "uses" in job, f"{path.name}:{name} has no runs-on/uses"
        steps = job.get("steps")
        if steps is None:
            continue
        assert isinstance(steps, list) and steps, f"{path.name}:{name} has an empty steps list"
        for index, step in enumerate(steps):
            assert isinstance(step, dict), f"{path.name}:{name} step #{index + 1} is not a mapping"
            assert "run" in step or "uses" in step, (
                f"{path.name}:{name} step #{index + 1} has neither run nor uses"
            )
