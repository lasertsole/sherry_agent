"""Bound skills ride the dispatch: a step's SKILL.md files precede its task.

``step["skills"] = ["browser"]`` means the child agent receives the browser
skill's full SKILL.md ahead of the step's own instruction text, so the main
agent never has to ``skill_view`` it into its own context just to relay it.
Pinned here: the prepend order, the byte-identical no-binding path, and the
fail-open behaviour for a binding that no longer loads.
"""

from __future__ import annotations

import json

import pytest

from agent.tools.taskflow.tools._shared import build_task_with_dep_results, new_step
from agent.tools.taskflow.tools._skill_prepend import prepend_skills

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Parsed(dict):
    """Stands in for the JSON dict ``_skill_view`` returns."""


def _view_returning(name: str, *, success: bool = True, content: str = "") -> str:
    return json.dumps(
        {"success": success, "content": content, "error": None if success else "not found"}
    )


@pytest.fixture
def fake_skill_view(monkeypatch):
    """Patch the canonical loader: 'browser' loads, 'ghost' does not."""
    calls: list[tuple[str, str]] = []

    def _fake(name: str, caller_scope: str = "main") -> str:
        calls.append((name, caller_scope))
        if name == "ghost":
            return _view_returning(name, success=False)
        if name == "empty":
            return _view_returning(name, content="")
        return _view_returning(name, content=f"# {name} SKILL.md\ninstructions for {name}")

    import agent.tools.skill_tools.skill_view as skill_view_mod

    monkeypatch.setattr(skill_view_mod, "_skill_view", _fake)
    bumped: list[str] = []
    import agent.tools.pub_base.skill_usage as usage_mod

    monkeypatch.setattr(usage_mod, "bump_use", lambda name: bumped.append(name))
    return {"calls": calls, "bumped": bumped}


class TestPrependSkills:
    def test_no_binding_returns_the_task_unchanged(self, fake_skill_view):
        assert prepend_skills("do the thing", None) == "do the thing"
        assert prepend_skills("do the thing", []) == "do the thing"
        assert fake_skill_view["calls"] == []

    def test_a_bound_skill_precedes_the_task(self, fake_skill_view):
        out = prepend_skills("do the thing", ["browser"])

        assert out.startswith('[IMPORTANT: The user has invoked the "browser" skill.')
        assert "# browser SKILL.md" in out
        assert out.endswith("\n\ndo the thing"), "the task text stays LAST"
        assert fake_skill_view["calls"] == [("browser", "background")]

    def test_several_bindings_keep_their_order(self, fake_skill_view):
        out = prepend_skills("task", ["browser", "code-wiki"])

        assert out.index('"browser" skill') < out.index('"code-wiki" skill')
        assert out.endswith("\n\ntask")

    def test_an_unloadable_binding_is_skipped(self, fake_skill_view):
        out = prepend_skills("task", ["ghost"])

        assert out == "task", "fail-open: a stale binding never blocks the dispatch"

    def test_a_mixed_list_keeps_the_loadable_half(self, fake_skill_view):
        out = prepend_skills("task", ["browser", "ghost", "empty"])

        assert '"browser" skill' in out
        assert "ghost" not in out
        assert out.endswith("\n\ntask")

    def test_usage_is_bumped_for_every_loaded_skill(self, fake_skill_view):
        prepend_skills("task", ["browser", "code-wiki", "ghost"])

        assert fake_skill_view["bumped"] == ["browser", "code-wiki"]

    def test_a_loader_exception_is_fail_open(self, monkeypatch):
        import agent.tools.skill_tools.skill_view as skill_view_mod

        def _boom(name: str, caller_scope: str = "main") -> str:
            raise RuntimeError("loader exploded")

        monkeypatch.setattr(skill_view_mod, "_skill_view", _boom)

        assert prepend_skills("task", ["browser"]) == "task"


class TestDispatchIntegration:
    def test_the_task_builder_prepends_bound_skills(self, fake_skill_view):
        step = new_step("step-1", "write the e2e test", skills=["browser"])

        out = build_task_with_dep_results(step, [step], [])

        assert out.startswith('[IMPORTANT: The user has invoked the "browser" skill.')
        assert out.endswith("write the e2e test")

    def test_a_step_without_skills_is_byte_identical(self, fake_skill_view):
        step = new_step("step-1", "plain task")

        assert build_task_with_dep_results(step, [step], []) == "plain task"
        assert fake_skill_view["calls"] == []

    def test_upstream_results_come_after_the_skill_and_task(self, fake_skill_view):
        dep = new_step("step-0", "first", status="done")
        dep["child_session_key"] = "child-0"
        step = new_step("step-1", "second", depends_on=["step-0"], skills=["browser"])
        step["aggregate_deps"] = True

        out = build_task_with_dep_results(
            step, [dep, step], [{"child_session_key": "child-0", "result": "RESULT-BODY"}]
        )

        assert out.index('"browser" skill') < out.index("second")
        assert out.index("second") < out.index("## Upstream Results")
        assert out.index("## Upstream Results") < out.index("RESULT-BODY")

    def test_new_step_accepts_the_skills_field(self):
        step = new_step("step-1", "task", skills=["browser"])

        assert step["skills"] == ["browser"]

    def test_new_step_refuses_an_unknown_field(self):
        with pytest.raises(ValueError):
            new_step("step-1", "task", skills_typo=["browser"])
