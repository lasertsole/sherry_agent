"""WorkMode stage 4: reusable TaskFlow plan templates.

Pins the template store's name validation, structure-only extraction (no runtime
fields), atomic save/load/list/delete, corrupt-file handling, and the merged
tool's save_as_template/load_from_template actions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow import template_store
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import taskflow_merged

pytestmark = [pytest.mark.unit]

_SESSION = "sess-tmpl"


@pytest.fixture
def template_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "templates"
    directory.mkdir()
    monkeypatch.setattr(template_store, "_templates_dir", lambda: directory)
    return directory


def _state(steps: list[dict]) -> dict:
    return {"description": "plan", "steps": steps, "results": []}


class TestStore:
    @pytest.mark.parametrize("bad", ["", "..", "../evil", "a/b", "x" * 65, "has space"])
    def test_invalid_names_rejected(self, bad):
        with pytest.raises(template_store.TemplateStoreError):
            template_store._validate_name(bad)

    @pytest.mark.asyncio
    async def test_save_load_roundtrip_structure_only(self, template_dir: Path):
        steps = [
            {
                "step_id": "step-1",
                "task": "do it",
                "depends_on": [],
                "status": "done",
                "retry_count": 3,
                "child_session_key": "secret-child",
                "judge_criteria": "must be green",
                "functional_role": "executor",
                "self_evaluate": True,
            }
        ]
        result = await template_store.save_template("my-plan", _state(steps), description="demo")
        assert result["success"] and result["steps"] == 1
        loaded = template_store.load_template("my-plan")
        assert loaded is not None
        saved_step = loaded["steps"][0]
        assert saved_step["step_id"] == "step-1"
        assert saved_step["judge_criteria"] == "must be green"
        assert saved_step["functional_role"] == "executor"
        assert saved_step["self_evaluate"] is True
        # Runtime fields must NOT leak into the template.
        for forbidden in ("status", "retry_count", "child_session_key", "judge_feedback"):
            assert forbidden not in saved_step

    @pytest.mark.asyncio
    async def test_empty_flow_refuses(self, template_dir: Path):
        with pytest.raises(template_store.TemplateStoreError, match="no steps"):
            await template_store.save_template("empty", _state([]))

    def test_load_missing_is_none(self, template_dir: Path):
        assert template_store.load_template("nope") is None

    def test_load_corrupt_is_none(self, template_dir: Path):
        (template_dir / "bad.json").write_text("{not json", encoding="utf-8")
        assert template_store.load_template("bad") is None

    @pytest.mark.asyncio
    async def test_list_and_delete(self, template_dir: Path):
        await template_store.save_template(
            "alpha", _state([{"step_id": "s1", "task": "x", "depends_on": []}])
        )
        await template_store.save_template(
            "beta", _state([{"step_id": "s1", "task": "x", "depends_on": []}])
        )
        names = [item["name"] for item in template_store.list_templates()]
        assert names == ["alpha", "beta"]
        assert await template_store.delete_template("alpha") is True
        assert await template_store.delete_template("alpha") is False
        assert [item["name"] for item in template_store.list_templates()] == ["beta"]

    def test_name_cannot_escape_directory(self, template_dir: Path):
        with pytest.raises(template_store.TemplateStoreError):
            template_store._template_path("../../etc/passwd")


@pytest.mark.asyncio
class TestMergedActions:
    async def test_save_then_load_creates_fresh_flow(
        self, isolated_db: Path, template_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        tools = {t.name: t for t in taskflow_merged.TASKFLOW_MERGED_TOOLS}
        flow = tools["taskflow_flow"]
        # create a source flow with one step
        out = await flow.ainvoke(
            {
                "action": "create",
                "flow_id": "source-flow",
                "description": "source",
                "session_id": _SESSION,
            }
        )
        assert "created" in str(out).lower() or "revision" in str(out).lower(), out
        stored = await store_sqlite.get_flow("source-flow", _SESSION)
        assert stored is not None
        # put a step skeleton on it directly
        state = stored["state"]
        state["steps"] = [
            {
                "step_id": "step-1",
                "task": "do work",
                "depends_on": [],
                "status": "done",
                "retry_count": 2,
                "child_session_key": "secret",
                "judge_criteria": "green",
            }
        ]
        await store_sqlite.update_flow(
            "source-flow", stored["expected_revision"], session_id=_SESSION, state=state
        )

        saved = await flow.ainvoke(
            {
                "action": "save_as_template",
                "flow_id": "source-flow",
                "name": "reusable",
                "description": "demo",
                "session_id": _SESSION,
            }
        )
        assert "Template saved" in str(saved), saved

        loaded = await flow.ainvoke(
            {
                "action": "load_from_template",
                "name": "reusable",
                "flow_id": "loaded-flow",
                "description": "from template",
                "session_id": _SESSION,
            }
        )
        assert "revision" in str(loaded).lower(), loaded
        new_flow = await store_sqlite.get_flow("loaded-flow", _SESSION)
        assert new_flow is not None
        step = new_flow["state"]["steps"][0]
        assert step["task"] == "do work"
        assert step["judge_criteria"] == "green"
        assert step["status"] == "ready"
        assert "child_session_key" not in step
        assert "retry_count" not in step or step["retry_count"] == 0

    async def test_load_missing_template_errors(self, isolated_db: Path, template_dir: Path):
        tools = {t.name: t for t in taskflow_merged.TASKFLOW_MERGED_TOOLS}
        out = await tools["taskflow_flow"].ainvoke(
            {
                "action": "load_from_template",
                "name": "does-not-exist",
                "flow_id": "f-missing",
                "session_id": _SESSION,
            }
        )
        assert "Error" in str(out) and "not found" in str(out), out
