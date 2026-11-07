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
