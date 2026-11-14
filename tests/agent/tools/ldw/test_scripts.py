"""LDW script round-trip: Python literals, booleans/null, unicode payloads."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.ldw.scripts import find_ldw_script, load_ldw_script, write_ldw_script

pytestmark = [pytest.mark.unit]


def test_script_round_trips_python_literals(tmp_path: Path) -> None:
    meta = {
        "plan_key": "k1",
        "plan_name": "计划",
        "flow_id": "",
        "steps": [
            {
                "step_id": "a",
                "task_template": "do a",
                "agent_type": "general@a",
                "response_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "additionalProperties": False,
                },
                "step_model": None,
            }
        ],
    }
    path = write_ldw_script("k1", meta, root=tmp_path)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "false" not in text
    assert "None" in text
    assert find_ldw_script("k1", root=tmp_path) == path.resolve()
    assert load_ldw_script("k1", root=tmp_path) == meta


def test_script_name_is_sanitized(tmp_path: Path) -> None:
    assert write_ldw_script("../escape", {"steps": []}, root=tmp_path) is None
    assert find_ldw_script("../escape", root=tmp_path) is None


def test_a_tampered_script_is_refused_without_executing_it(tmp_path: Path) -> None:
    """M15: the loader parses data, it never ``exec_module``s the file."""
    scripts = tmp_path / "learned" / "scripts"
    scripts.mkdir(parents=True)
    marker = tmp_path / "pwned"
    (scripts / "ldw__k1.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('pwned')\n"
        "WORKFLOW_META = {'plan_key': 'k1'}\n",
        encoding="utf-8",
    )

    assert load_ldw_script("k1", root=tmp_path) is None
    assert not marker.exists(), "the loader must never execute the script"


def test_only_literal_constant_assignments_load(tmp_path: Path) -> None:
    scripts = tmp_path / "learned" / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "ldw__k1.py"
    target.write_text("WORKFLOW_META = dict(plan_key='k1')\n", encoding="utf-8")
    assert load_ldw_script("k1", root=tmp_path) is None

    target.write_text("EVIL = 1\nWORKFLOW_META = {'plan_key': 'k1'}\n", encoding="utf-8")
    assert load_ldw_script("k1", root=tmp_path) is None

    target.write_text("WORKFLOW_META = {'plan_key': 'k1'}\n", encoding="utf-8")
    assert load_ldw_script("k1", root=tmp_path) == {"plan_key": "k1"}
