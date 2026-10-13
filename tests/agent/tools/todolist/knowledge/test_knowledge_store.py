"""Unit tests for KnowledgeStore: identity-keyed plan knowledge persistence.

The store root is redirected to ``tmp_path`` through the module-level
``_KNOWLEDGE_ROOT`` seam, so the real ``workspace/knowledge/plans/`` tree is
never touched. Documents now live under the plan identity's ``<plan_key>/``
directory (identity is constructed directly here — its resolution is covered
by ``test_knowledge_identity.py``), so assertions use the key. Writes are
checked for the schema envelope (schema_version / plan_name / extracted_at)
plus ``meta.json``; reads cover the legacy name-keyed fallback; invalid
selectors cover the error contract.
"""

import json
from pathlib import Path

import pytest

from agent.tools.todolist.knowledge.identity import PlanIdentity
from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

pytestmark = [pytest.mark.unit]

_KEY = "0123456789ab"


def _identity(plan_name: str = "p", key: str = _KEY) -> PlanIdentity:
    """A directly constructed identity (resolution is tested elsewhere)."""
    return PlanIdentity(
        key=key,
        plan_name=plan_name,
        plan_ref=Path("/repo/workspace/sessions/s/plans") / f"{plan_name}.md",
    )


@pytest.fixture()
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a per-test knowledge root."""
    root = tmp_path / "plans"
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store._KNOWLEDGE_ROOT",
        root,
    )
    return root


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_task_layer_creates_sidecar_json(self, knowledge_root: Path):
        path = await KnowledgeStore.write(
            _identity("implement-auth", key="aaa111bbb222"),
            layer="task",
            data={"method": "passlib-async", "failure_set": ["sync bcrypt failed"]},
            position=0,
        )

        written = Path(path)
        assert written == knowledge_root / "aaa111bbb222" / "task-0.json"
        payload = json.loads(written.read_text(encoding="utf-8"))
        assert payload["method"] == "passlib-async"
        assert payload["failure_set"] == ["sync bcrypt failed"]
        assert payload["schema_version"] == 1
        assert payload["plan_name"] == "implement-auth"
        assert len(payload["extracted_at"]) == 14

    @pytest.mark.asyncio
    async def test_write_plan_layer_uses_plan_summary_filename(self, knowledge_root: Path):
        path = await KnowledgeStore.write(_identity(), layer="plan", data={"method": "m"})

        assert Path(path).name == "plan-summary.json"
        assert Path(path).parent.name == _KEY

    @pytest.mark.asyncio
    async def test_write_does_not_mutate_caller_data(self, knowledge_root: Path):
        data = {"method": "m"}

        await KnowledgeStore.write(_identity(), layer="plan", data=data)

        assert data == {"method": "m"}

    @pytest.mark.asyncio
    async def test_write_records_meta_json(
        self, knowledge_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from config import path as config_path

        monkeypatch.setattr(config_path, "ROOT_DIR", Path("/repo"))
        identity = PlanIdentity(
            key=_KEY,
            plan_name="p",
            plan_ref=Path("/repo/workspace/sessions/s/plans/p.md"),
        )

        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        meta = json.loads((knowledge_root / _KEY / "meta.json").read_text(encoding="utf-8"))
        assert meta["key"] == _KEY
        assert meta["plan_name"] == "p"
        assert meta["plan_ref"] == "workspace/sessions/s/plans/p.md"
        assert meta["schema_version"] == 1
        assert len(meta["updated_at"]) == 14

    @pytest.mark.asyncio
    async def test_write_task_layer_requires_position(self, knowledge_root: Path):
        with pytest.raises(ValueError, match="position"):
            await KnowledgeStore.write(_identity(), layer="task", data={})

    @pytest.mark.asyncio
    async def test_write_wave_layer_requires_wave_index(self, knowledge_root: Path):
        with pytest.raises(ValueError, match="wave_index"):
            await KnowledgeStore.write(_identity(), layer="wave", data={})

    @pytest.mark.asyncio
    async def test_write_unknown_layer_raises(self, knowledge_root: Path):
        with pytest.raises(ValueError, match="unknown layer"):
            await KnowledgeStore.write(_identity(), layer="bogus", data={})  # type: ignore[arg-type]


class TestReadAll:
    @pytest.mark.asyncio
    async def test_read_all_buckets_tasks_waves_and_plan(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="task", data={"method": "t0"}, position=0)
        await KnowledgeStore.write(identity, layer="task", data={"method": "t1"}, position=1)
        await KnowledgeStore.write(identity, layer="wave", data={"task_count": 2}, wave_index=0)
        await KnowledgeStore.write(identity, layer="plan", data={"method": "plan"})

        result = KnowledgeStore.read_all(identity)

        assert set(result["tasks"]) == {0, 1}
        assert result["tasks"][1]["method"] == "t1"
        assert set(result["waves"]) == {0}
        assert result["plan"]["method"] == "plan"

    def test_read_all_returns_empty_dict_for_unknown_plan(self, knowledge_root: Path):
        assert KnowledgeStore.read_all(_identity("nope")) == {}

    def test_read_all_skips_malformed_and_unrelated_files(self, knowledge_root: Path):
        plan_dir = knowledge_root / _KEY
        plan_dir.mkdir(parents=True)
        (plan_dir / "task-0.json").write_text("{not json", encoding="utf-8")
        (plan_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
        (plan_dir / "task-x.json").write_text("{}", encoding="utf-8")

        result = KnowledgeStore.read_all(_identity())

        assert result["tasks"] == {}
        assert result["waves"] == {}
        assert result["plan"] is None

    def test_read_all_falls_back_to_legacy_name_directory(self, knowledge_root: Path):
        legacy_dir = knowledge_root / "p"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "plan-summary.json").write_text(
            json.dumps({"method": "legacy-method"}), encoding="utf-8"
        )

        result = KnowledgeStore.read_all(_identity())

        assert result["plan"]["method"] == "legacy-method"

    @pytest.mark.asyncio
    async def test_key_directory_wins_over_legacy(self, knowledge_root: Path):
        legacy_dir = knowledge_root / "p"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "plan-summary.json").write_text(
            json.dumps({"method": "legacy-method"}), encoding="utf-8"
        )
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "key-method"})

        result = KnowledgeStore.read_all(identity)

        assert result["plan"]["method"] == "key-method"


class TestReadSummary:
    @pytest.mark.asyncio
    async def test_read_summary_returns_plan_document(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(
            identity, layer="plan", data={"method": "m", "key_failures": ["f"]}
        )

        summary = KnowledgeStore.read_summary(identity)

        assert summary is not None
        assert summary["method"] == "m"
        assert summary["key_failures"] == ["f"]

    def test_read_summary_returns_none_when_missing(self, knowledge_root: Path):
        assert KnowledgeStore.read_summary(_identity("nope")) is None

    def test_read_summary_returns_none_when_malformed(self, knowledge_root: Path):
        plan_dir = knowledge_root / _KEY
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan-summary.json").write_text("[1, 2", encoding="utf-8")

        assert KnowledgeStore.read_summary(_identity()) is None

    def test_read_summary_falls_back_to_legacy_name_directory(self, knowledge_root: Path):
        legacy_dir = knowledge_root / "p"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "plan-summary.json").write_text(
            json.dumps({"method": "legacy"}), encoding="utf-8"
        )

        summary = KnowledgeStore.read_summary(_identity())

        assert summary is not None and summary["method"] == "legacy"


class TestReadFormatted:
    @pytest.mark.asyncio
    async def test_overview_has_plan_summary_and_task_index(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(
            identity,
            layer="plan",
            data={
                "method": "async-hashing",
                "total_waves": 1,
                "total_tasks": 1,
                "key_failures": ["sync bcrypt failed"],
                "key_successes": ["passlib context worked"],
                "reusable_patterns": ["CryptContext(schemes=['bcrypt'])"],
            },
        )
        await KnowledgeStore.write(
            identity,
            layer="task",
            data={"method": "task-method", "failure_set": ["a"], "success_path": ["b", "c"]},
            position=0,
        )

        text = KnowledgeStore.read_formatted(identity)

        assert "# Knowledge: p" in text
        assert "Method: async-hashing" in text
        assert "sync bcrypt failed" in text
        assert "passlib context worked" in text
        assert "CryptContext(schemes=['bcrypt'])" in text
        assert "Task 0: method=task-method (failures=1, steps=2)" in text
        assert "layer='task', position=N" in text

    @pytest.mark.asyncio
    async def test_plan_layer_returns_full_summary_without_task_index(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})
        await KnowledgeStore.write(
            identity, layer="task", data={"method": "task-method"}, position=0
        )

        text = KnowledgeStore.read_formatted(identity, layer="plan")

        assert "Method: m" in text
        assert "Task Index" not in text

    @pytest.mark.asyncio
    async def test_task_detail_lists_failure_set_success_path_and_runs(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(
            identity,
            layer="task",
            data={
                "method": "task-method",
                "failure_set": ["boom"],
                "success_path": ["step one"],
                "subagent_runs": [{"task_name": "impl", "outcome": "ok"}],
            },
            position=0,
        )

        text = KnowledgeStore.read_formatted(identity, layer="task", position=0)

        assert "# Task 0 Knowledge: p" in text
        assert "1. boom" in text
        assert "1. step one" in text
        assert "- impl: outcome=ok" in text

    @pytest.mark.asyncio
    async def test_task_layer_without_position_reports_error(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        text = KnowledgeStore.read_formatted(identity, layer="task")

        assert text == "Error: layer='task' requires position"

    @pytest.mark.asyncio
    async def test_task_layer_without_document_reports_not_found(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        text = KnowledgeStore.read_formatted(identity, layer="task", position=7)

        assert text == "No knowledge found for task 7 in p"

    @pytest.mark.asyncio
    async def test_wave_detail_lists_patterns_and_task_summaries(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(
            identity,
            layer="wave",
            data={
                "task_count": 1,
                "wave_failure_patterns": ["all sync"],
                "wave_success_patterns": ["async unlocked"],
                "tasks_summary": [{"position": 0, "method": "m0"}],
            },
            wave_index=0,
        )

        text = KnowledgeStore.read_formatted(identity, layer="wave", wave_index=0)

        assert "# Wave 0 Knowledge: p" in text
        assert "all sync" in text
        assert "async unlocked" in text
        assert "Task 0: method=m0" in text

    @pytest.mark.asyncio
    async def test_wave_layer_without_wave_index_reports_error(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        text = KnowledgeStore.read_formatted(identity, layer="wave")

        assert text == "Error: layer='wave' requires wave_index"

    def test_unknown_plan_reports_not_found(self, knowledge_root: Path):
        assert (
            KnowledgeStore.read_formatted(_identity("nope")) == "No knowledge found for plan: nope"
        )

    @pytest.mark.asyncio
    async def test_unknown_layer_reports_error(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        assert KnowledgeStore.read_formatted(identity, layer="bogus") == "Unknown layer: bogus"


class TestHasKnowledge:
    @pytest.mark.asyncio
    async def test_true_for_key_directory(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})

        assert KnowledgeStore.has_knowledge(identity) is True

    def test_true_for_legacy_directory(self, knowledge_root: Path):
        (knowledge_root / "p").mkdir(parents=True)

        assert KnowledgeStore.has_knowledge(_identity()) is True

    def test_false_when_absent(self, knowledge_root: Path):
        assert KnowledgeStore.has_knowledge(_identity()) is False


class TestPurge:
    @pytest.mark.asyncio
    async def test_purge_removes_key_directory_only(self, knowledge_root: Path):
        identity = _identity()
        await KnowledgeStore.write(identity, layer="plan", data={"method": "m"})
        legacy_dir = knowledge_root / "p"
        legacy_dir.mkdir(parents=True)

        removed = KnowledgeStore.purge(identity)

        assert removed is True
        assert (knowledge_root / _KEY).exists() is False
        assert legacy_dir.is_dir() is True

    def test_purge_returns_false_when_missing(self, knowledge_root: Path):
        assert KnowledgeStore.purge(_identity()) is False
