"""The planner carries `skills` bindings from its JSON into the step list."""

from __future__ import annotations

import importlib
import json

import pytest

from agent.tools.taskflow.tools._shared import build_task_with_dep_results, new_step

pytestmark = [pytest.mark.unit]

# The package attribute of this name is the TOOL; the module comes via sys.modules.
plan_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_plan")


class TestPlanSkillsBinding:
    def test_parse_plan_keeps_the_skills_field(self):
        raw = json.dumps(
            [
                {"step_id": "step-1", "task": "design", "depends_on": []},
                {
                    "step_id": "step-2",
                    "task": "write e2e",
                    "depends_on": ["step-1"],
                    "skills": ["browser", "code-wiki"],
                },
            ]
        )

        plan = plan_mod._parse_plan(raw)

        assert plan[0]["skills"] is None, "absent = no binding"
        assert plan[1]["skills"] == ["browser", "code-wiki"]

    def test_the_prompt_documents_the_field(self):
        assert "skills" in plan_mod._SYSTEM_PROMPT
        assert "prepends" in plan_mod._SYSTEM_PROMPT

    def test_parse_plan_keeps_file_deps(self):
        raw = json.dumps(
            [
                {
                    "step_id": "step-1",
                    "task": "verify alpha",
                    "depends_on": [],
                    "file_deps": ["alpha.py", "src/core.py"],
                }
            ]
        )

        plan = plan_mod._parse_plan(raw)

        assert plan[0]["file_deps"] == ["alpha.py", "src/core.py"]

    def test_the_prompt_documents_file_deps(self):
        assert "file_deps" in plan_mod._SYSTEM_PROMPT
        assert "regression gate" in plan_mod._SYSTEM_PROMPT

    def test_a_malformed_file_deps_value_is_dropped(self):
        raw = json.dumps(
            [
                {
                    "step_id": "step-1",
                    "task": "verify alpha",
                    "depends_on": [],
                    "file_deps": "alpha.py",
                }
            ]
        )

        plan = plan_mod._parse_plan(raw)

        assert plan[0]["file_deps"] is None

    def test_planned_steps_carry_the_binding_into_the_dispatch_text(self, monkeypatch):
        # What taskflow_plan writes (new_step(..., skills=...)) is exactly what
        # the dispatch builder consumes.
        step = new_step("step-2", "write e2e", skills=["browser"])
        calls: list[str] = []

        def _fake_view(name: str, caller_scope: str = "main") -> str:
            calls.append(name)
            return json.dumps({"success": True, "content": f"# {name}"})

        import agent.tools.skill_tools.skill_view as skill_view_mod

        monkeypatch.setattr(skill_view_mod, "_skill_view", _fake_view)

        out = build_task_with_dep_results(step, [step], [])

        assert calls == ["browser"]
        assert out.startswith('[IMPORTANT: The user has invoked the "browser" skill.')
        assert out.endswith("write e2e")
