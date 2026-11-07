"""Skill-script lookup and PTC script static validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.ptc.compiler import validate_ptc_script
from agent.tools.ptc.scripts import (
    find_step_script,
    is_safe_step_id,
    read_step_script,
    script_filename,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_PLAN = "0123456789ab"
_STEP = "step-1"


def _write(root: Path, rel: str, text: str = "print('x')\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_script_filename_is_exact_and_rejects_unsafe_ids() -> None:
    assert script_filename(_PLAN, _STEP) == f"ptc__{_PLAN}__{_STEP}.py"
    assert is_safe_step_id("step-1.2_a")
    assert not is_safe_step_id("../step")
    assert not is_safe_step_id("step/one")
    assert not is_safe_step_id("")
    assert script_filename(_PLAN, "../step") is None


def test_find_returns_the_unique_top_level_script(tmp_path: Path) -> None:
    path = _write(tmp_path, f"cat/my-skill/scripts/ptc__{_PLAN}__{_STEP}.py")

    assert find_step_script(_PLAN, _STEP, root=tmp_path) == path.resolve()


def test_nested_scripts_directory_is_not_a_hit(tmp_path: Path) -> None:
    _write(tmp_path, f"cat/my-skill/scripts/ptc/ptc__{_PLAN}__{_STEP}.py")

    assert find_step_script(_PLAN, _STEP, root=tmp_path) is None


def test_ambiguous_scripts_fail_open(tmp_path: Path) -> None:
    _write(tmp_path, f"a/one/scripts/ptc__{_PLAN}__{_STEP}.py")
    _write(tmp_path, f"b/two/scripts/ptc__{_PLAN}__{_STEP}.py")

    assert find_step_script(_PLAN, _STEP, root=tmp_path) is None


def test_missing_or_unsafe_script_returns_none(tmp_path: Path) -> None:
    assert find_step_script(_PLAN, _STEP, root=tmp_path) is None
    assert find_step_script(_PLAN, "../step", root=tmp_path) is None


def test_read_caps_the_script_size(tmp_path: Path) -> None:
    _write(tmp_path, f"a/one/scripts/ptc__{_PLAN}__{_STEP}.py", "x" * 100)

    assert read_step_script(_PLAN, _STEP, root=tmp_path, max_bytes=10) is None
    assert read_step_script(_PLAN, _STEP, root=tmp_path, max_bytes=200) == "x" * 100


def test_symlink_candidate_is_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path, "outside.py", "print('x')\n")
    link = tmp_path / f"a/one/scripts/ptc__{_PLAN}__{_STEP}.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    assert find_step_script(_PLAN, _STEP, root=tmp_path) is None


def test_valid_script_passes_static_validation() -> None:
    code = (
        "from sherry_tools import read_file\n"
        "import json\n"
        "data = read_file(file_path='README.md')\n"
        "print(json.dumps(data))\n"
    )
    assert validate_ptc_script(code, allowed_tools={"read_file"}) is None


@pytest.mark.parametrize(
    "code, needle",
    [
        ("import os\nprint('x')\n", "import not allowed"),
        ("from sherry_tools import execute_code\nprint('x')\n", "tool not present"),
        ("from sherry_tools import read_file\n", "must print"),
        ("print('x')\n", "sherry_tools"),
        ("open('x')\nprint('x')\n", "forbidden"),
        ("from sherry_tools import read_file\nprint(input())\n", "forbidden"),
        ("from sherry_tools import read_file\nprint(execute_code())\n", "forbidden"),
    ],
)
def test_static_validation_rejects_bad_scripts(code: str, needle: str) -> None:
    error = validate_ptc_script(code, allowed_tools={"read_file"})
    assert error is not None
    assert needle in error


def test_static_validation_caps_size() -> None:
    code = "from sherry_tools import read_file\nprint('x')\n"
    assert validate_ptc_script(code, allowed_tools={"read_file"}, max_bytes=10) is not None
