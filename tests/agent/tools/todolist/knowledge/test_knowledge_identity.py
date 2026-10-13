"""Plan-identity tests: physical isolation, collaboration, legacy, fallback, purge.

These lock the contract behind ``identity.py`` and its store/tool consumers:

- same literal plan name, different plan files -> two different ``<plan_key>``
  directories, neither write overwrites the other (the core fix);
- one plan file shared through boulder ``session_ids`` -> all listed sessions
  resolve one key and read each other's documents;
- a session's own ``plan_ref`` disambiguates when two paths carry the name;
- the session-derived fallback name hashes the FULL id, so ids sharing an
  8-char prefix stay isolated;
- legacy ``<plan-name>`` directories stay readable; writes migrate to the key;
- ``clear_session`` purges private identities and keeps shared ones;
- ``KnowledgeStore.list_plans(session)`` returns only the session's plans.
"""

import json
from pathlib import Path

import pytest

from agent.tools.todolist.knowledge import build_knowledge_tools, ownership
from agent.tools.todolist.knowledge import knowledge_store as store_mod
from agent.tools.todolist.knowledge.identity import (
    PlanIdentity,
    associated_plan_identities,
    fallback_plan_name,
    plan_key,
    resolve_plan_identity,
    resolve_session_plan_identity,
)
from agent.tools.todolist.knowledge.knowledge_store import (
    KnowledgeStore,
    clear_session_plan_knowledge,
)

pytestmark = [pytest.mark.unit]


class _FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


class _Env:
    """Bundle of the patched seams for one test."""

    def __init__(
        self,
        state_db: _FakeStateDB,
        todos: dict[str, list[dict]],
        boulder_path: Path,
        root: Path,
        repo: Path,
    ) -> None:
        self.state_db = state_db
        self.todos = todos
        self.boulder_path = boulder_path
        self.root = root
        self.repo = repo

    def associate_state(self, session_id: str, plan_name: str) -> None:
        self.state_db.set_state(
            session_id, "plan_ref", f"workspace/sessions/{session_id}/plans/{plan_name}.md"
        )

    def associate_todos(self, session_id: str, *plan_refs: str) -> None:
        self.todos[session_id] = [{"content": "x", "plan_ref": ref} for ref in plan_refs]

    def write_boulder(self, works: list[tuple[str, list[str], str | None]]) -> None:
        payload = {
            "schema_version": 2,
            "works": {
                f"work-{index}": {
                    "plan_name": plan_name,
                    "session_ids": session_ids,
                    **({"active_plan": active_plan} if active_plan else {}),
                }
                for index, (plan_name, session_ids, active_plan) in enumerate(works)
            },
        }
        self.boulder_path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """Isolate every seam: sources, knowledge root, and the repo root (tmp)."""
    from config import path as config_path

    state_db = _FakeStateDB()
    todos: dict[str, list[dict]] = {}
    boulder_path = tmp_path / "boulder.json"
    root = tmp_path / "knowledge" / "plans"
    repo = tmp_path / "repo"
    (repo / "workspace" / "sessions").mkdir(parents=True)

    monkeypatch.setattr(ownership, "state_register_db", state_db)
    monkeypatch.setattr(
        ownership, "get_todos_sync", lambda session_id: list(todos.get(session_id, []))
    )
    monkeypatch.setattr(ownership, "resolve_boulder_path", lambda: boulder_path)
    monkeypatch.setattr(store_mod, "_KNOWLEDGE_ROOT", root)
    monkeypatch.setattr(config_path, "ROOT_DIR", repo)
    monkeypatch.setattr(config_path, "SESSIONS_DIR", repo / "workspace" / "sessions")
    return _Env(state_db, todos, boulder_path, root, repo)


def _knowledge_tool():
    return {t.name: t for t in build_knowledge_tools()}["knowledge"]


class TestResolution:
    def test_same_name_different_plan_files_resolve_to_different_keys(self, env: _Env):
        env.associate_state("sess-a", "x")
        env.associate_state("sess-b", "x")

        identity_a = resolve_plan_identity("sess-a", "x")
        identity_b = resolve_plan_identity("sess-b", "x")

        assert identity_a is not None and identity_b is not None
        assert identity_a.key != identity_b.key
        assert identity_a.plan_ref == env.repo / "workspace/sessions/sess-a/plans/x.md"
        assert identity_b.plan_ref == env.repo / "workspace/sessions/sess-b/plans/x.md"

    def test_plan_key_is_repo_relative_and_spelling_stable(self, env: _Env):
        canonical = env.repo / "workspace" / "sessions" / "a" / "plans" / "x.md"
        meandering = (
            env.repo / "workspace" / "sessions" / ".." / "sessions" / "a" / "plans" / "x.md"
        )

        key = plan_key(canonical)

        assert plan_key(meandering) == key
        assert len(key) == 12 and all(char in "0123456789abcdef" for char in key)
        assert plan_key(env.repo / "workspace" / "sessions" / "b" / "plans" / "x.md") != key

    def test_unknown_name_without_any_source_resolves_to_none(self, env: _Env):
        env.associate_state("sess-a", "alpha")

        assert resolve_plan_identity("sess-a", "beta") is None

    def test_state_plan_ref_disambiguates_two_same_named_paths(self, env: _Env):
        env.associate_todos(
            "sess-a",
            "workspace/sessions/sess-a/plans/x.md",
            "workspace/sessions/sess-other/plans/x.md",
        )

        assert resolve_plan_identity("sess-a", "x") is None

        env.associate_state("sess-a", "x")
        identity = resolve_plan_identity("sess-a", "x")

        assert identity is not None
        assert identity.plan_ref == env.repo / "workspace/sessions/sess-a/plans/x.md"

    def test_session_plan_identity_prefers_state_then_todos_then_fallback(self, env: _Env):
        env.associate_todos("sess-todos", "workspace/sessions/sess-todos/plans/t.md")

        identity = resolve_session_plan_identity("sess-todos")

        assert identity is not None and identity.plan_name == "t"

        fallback = resolve_session_plan_identity("sess-bare")

        assert fallback is not None
        assert fallback.is_session_fallback is True
        assert fallback.plan_name == fallback_plan_name("sess-bare")


class TestSameNameIsolation:
    @pytest.mark.asyncio
    async def test_writes_are_physically_isolated_and_read_back_own_data(self, env: _Env):
        env.associate_state("sess-a", "x")
        env.associate_state("sess-b", "x")
        tool = _knowledge_tool()

        written_a = await tool.coroutine(
            action="write",
            plan_name="x",
            layer="plan",
            data={"method": "from-A"},
            session_id="sess-a",
        )
        written_b = await tool.coroutine(
            action="write",
            plan_name="x",
            layer="plan",
            data={"method": "from-B"},
            session_id="sess-b",
        )
        await tool.coroutine(
            action="write",
            plan_name="x",
            layer="task",
            position=0,
            data={"method": "task-B"},
            session_id="sess-b",
        )

        assert written_a.startswith("Knowledge written to ")
        assert written_b.startswith("Knowledge written to ")
        identity_a = resolve_plan_identity("sess-a", "x")
        identity_b = resolve_plan_identity("sess-b", "x")
        assert identity_a is not None and identity_b is not None
        assert (env.root / identity_a.key / "plan-summary.json").is_file()
        assert (env.root / identity_b.key / "plan-summary.json").is_file()
        assert identity_a.key != identity_b.key
        assert len([entry for entry in env.root.iterdir() if entry.is_dir()]) == 2
        assert (env.root / "x").exists() is False

        read_a = await tool.coroutine(
            action="read", plan_name="x", layer="plan", session_id="sess-a"
        )
        read_b = await tool.coroutine(
            action="read", plan_name="x", layer="plan", session_id="sess-b"
        )
        task_a = await tool.coroutine(
            action="read", plan_name="x", layer="task", position=0, session_id="sess-a"
        )

        assert "from-A" in read_a and "from-B" not in read_a
        assert "from-B" in read_b and "from-A" not in read_b
        assert "task-B" not in task_a


class TestCollaboration:
    @pytest.mark.asyncio
    async def test_boulder_collaborators_share_one_key(self, env: _Env):
        env.write_boulder(
            [
                (
                    "shared",
                    ["sess-a", "sess-b"],
                    "workspace/sessions/sess-a/plans/shared.md",
                )
            ]
        )
        tool = _knowledge_tool()

        identity_a = resolve_plan_identity("sess-a", "shared")
        identity_b = resolve_plan_identity("sess-b", "shared")

        assert identity_a is not None and identity_b is not None
        assert identity_a.key == identity_b.key

        written_a = await tool.coroutine(
            action="write",
            plan_name="shared",
            layer="plan",
            data={"method": "from-A"},
            session_id="sess-a",
        )
        read_b = await tool.coroutine(
            action="read", plan_name="shared", layer="plan", session_id="sess-b"
        )

        assert written_a.startswith("Knowledge written to ")
        assert "from-A" in read_b
        assert len([entry for entry in env.root.iterdir() if entry.is_dir()]) == 1

        listed = await tool.coroutine(action="list", session_id="sess-b")

        assert "shared" in listed and identity_a.key in listed

    def test_boulder_active_plan_is_shared_without_state_refs(self, env: _Env):
        env.write_boulder(
            [("shared", ["sess-a", "sess-b"], "workspace/sessions/sess-a/plans/shared.md")]
        )

        identity_a = resolve_plan_identity("sess-a", "shared")
        identity_b = resolve_plan_identity("sess-b", "shared")

        assert identity_a is not None and identity_b is not None
        assert identity_a.key == identity_b.key

    def test_own_plan_ref_wins_over_boulder_shared_work(self, env: _Env):
        env.write_boulder([("x", ["sess-a", "sess-b"], "workspace/sessions/sess-a/plans/x.md")])
        env.associate_state("sess-b", "x")

        identity_a = resolve_plan_identity("sess-a", "x")
        identity_b = resolve_plan_identity("sess-b", "x")

        assert identity_a is not None and identity_b is not None
        assert identity_a.key != identity_b.key
        assert identity_b.plan_ref == env.repo / "workspace/sessions/sess-b/plans/x.md"

    def test_boulder_work_without_active_plan_anchors_to_first_listed_session(self, env: _Env):
        env.write_boulder([("legacy-shape", ["sess-a", "sess-b"], None)])

        identity_a = resolve_plan_identity("sess-a", "legacy-shape")
        identity_b = resolve_plan_identity("sess-b", "legacy-shape")

        assert identity_a is not None and identity_b is not None
        assert identity_a.key == identity_b.key
        assert identity_a.plan_ref == env.repo / "workspace/sessions/sess-a/plans/legacy-shape.md"


class TestFallbackIdentity:
    def test_fallback_name_hashes_the_full_session_id(self):
        name_a = fallback_plan_name("qq:user123456")
        name_b = fallback_plan_name("qq:user123457")

        assert name_a != name_b
        assert name_a.startswith("session-") and len(name_a) == len("session-") + 8

    def test_fallback_identity_only_accepts_the_own_session_name(self, env: _Env):
        own = resolve_plan_identity("sess-a", fallback_plan_name("sess-a"))
        foreign = resolve_plan_identity("sess-a", fallback_plan_name("sess-b"))

        assert own is not None and own.is_session_fallback is True
        assert foreign is None

    @pytest.mark.asyncio
    async def test_prefix_colliding_sessions_do_not_overwrite_each_other(self, env: _Env):
        sid_a, sid_b = "qq:user123456", "qq:user123457"
        tool = _knowledge_tool()
        name_a, name_b = fallback_plan_name(sid_a), fallback_plan_name(sid_b)

        assert sid_a[:8] == sid_b[:8]

        written_a = await tool.coroutine(
            action="write",
            plan_name=name_a,
            layer="plan",
            data={"method": "method-A"},
            session_id=sid_a,
        )
        written_b = await tool.coroutine(
            action="write",
            plan_name=name_b,
            layer="plan",
            data={"method": "method-B"},
            session_id=sid_b,
        )

        assert written_a.startswith("Knowledge written to ")
        assert written_b.startswith("Knowledge written to ")

        read_a = await tool.coroutine(
            action="read", plan_name=name_a, layer="plan", session_id=sid_a
        )
        read_b = await tool.coroutine(
            action="read", plan_name=name_b, layer="plan", session_id=sid_b
        )
        cross = await tool.coroutine(
            action="read", plan_name=name_b, layer="plan", session_id=sid_a
        )

        assert "method-A" in read_a and "method-B" not in read_a
        assert "method-B" in read_b and "method-A" not in read_b
        assert cross.startswith("Error: knowledge read denied")
        assert len([entry for entry in env.root.iterdir() if entry.is_dir()]) == 2


class TestLegacyFallback:
    @pytest.mark.asyncio
    async def test_legacy_dir_is_read_then_migrated_on_write(self, env: _Env):
        env.associate_state("sess-a", "x")
        legacy_dir = env.root / "x"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "plan-summary.json").write_text(
            json.dumps({"method": "legacy-method"}), encoding="utf-8"
        )
        tool = _knowledge_tool()

        read_legacy = await tool.coroutine(
            action="read", plan_name="x", layer="plan", session_id="sess-a"
        )
        identity = resolve_plan_identity("sess-a", "x")

        assert identity is not None
        assert "legacy-method" in read_legacy
        assert (env.root / identity.key).exists() is False

        await tool.coroutine(
            action="write",
            plan_name="x",
            layer="plan",
            data={"method": "new-method"},
            session_id="sess-a",
        )
        read_new = await tool.coroutine(
            action="read", plan_name="x", layer="plan", session_id="sess-a"
        )

        assert (env.root / identity.key / "plan-summary.json").is_file()
        assert "new-method" in read_new and "legacy-method" not in read_new
        assert legacy_dir.is_dir() is True


class TestClearSession:
    @pytest.mark.asyncio
    async def test_purges_private_identities_and_keeps_shared(self, env: _Env):
        env.associate_state("sess-a", "private-plan")
        env.write_boulder(
            [("shared", ["sess-a", "sess-b"], "workspace/sessions/sess-a/plans/shared.md")]
        )
        tool = _knowledge_tool()

        for plan_name in ("private-plan", "shared"):
            await tool.coroutine(
                action="write",
                plan_name=plan_name,
                layer="plan",
                data={"method": plan_name},
                session_id="sess-a",
            )
        await tool.coroutine(
            action="write",
            plan_name=fallback_plan_name("sess-a"),
            layer="plan",
            data={"method": "fallback"},
            session_id="sess-a",
        )

        private = resolve_plan_identity("sess-a", "private-plan")
        shared = resolve_plan_identity("sess-a", "shared")
        assert private is not None and shared is not None
        assert (env.root / private.key).is_dir()
        assert (env.root / shared.key).is_dir()

        removed = clear_session_plan_knowledge("sess-a")

        assert removed == 2
        assert (env.root / private.key).exists() is False
        assert (env.root / shared.key).is_dir() is True


class TestListPlans:
    @pytest.mark.asyncio
    async def test_list_returns_only_session_associated_plans(self, env: _Env):
        env.associate_state("sess-a", "plan-a")
        env.associate_state("sess-b", "plan-b")
        tool = _knowledge_tool()
        for session_id, plan_name in (("sess-a", "plan-a"), ("sess-b", "plan-b")):
            await tool.coroutine(
                action="write",
                plan_name=plan_name,
                layer="plan",
                data={"method": f"{plan_name}-method"},
                session_id=session_id,
            )

        plans_a = KnowledgeStore.list_plans("sess-a")
        plans_b = KnowledgeStore.list_plans("sess-b")

        assert [identity.plan_name for identity in plans_a] == ["plan-a"]
        assert [identity.plan_name for identity in plans_b] == ["plan-b"]

        listed_a = await tool.coroutine(action="list", session_id="sess-a")
        listed_b = await tool.coroutine(action="list", session_id="sess-b")

        assert "plan-a" in listed_a and "plan-a-method" in listed_a
        assert "plan-b" not in listed_a
        assert "plan-b" in listed_b and "plan-a" not in listed_b

    def test_associated_identities_include_fallback_and_dedupe(self, env: _Env):
        env.associate_state("sess-a", "x")
        env.associate_todos("sess-a", "workspace/sessions/sess-a/plans/x.md")

        identities = associated_plan_identities("sess-a")

        assert [identity.plan_name for identity in identities] == [
            "x",
            fallback_plan_name("sess-a"),
        ]
        assert len({identity.key for identity in identities}) == 2

    def test_empty_session_has_no_identities(self, env: _Env):
        assert associated_plan_identities("") == []
        assert resolve_session_plan_identity("") is None
        assert resolve_plan_identity("", "x") is None


class TestPlanKeyContract:
    def test_key_stays_stable_when_repo_moves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from config import path as config_path

        first_repo = tmp_path / "first"
        second_repo = tmp_path / "elsewhere" / "second"
        plan_ref = Path("workspace/sessions/a/plans/x.md")

        monkeypatch.setattr(config_path, "ROOT_DIR", first_repo)
        key_first = plan_key(first_repo / plan_ref)
        monkeypatch.setattr(config_path, "ROOT_DIR", second_repo)
        key_second = plan_key(second_repo / plan_ref)

        assert key_first == key_second

    def test_identity_dataclass_marks_fallback(self):
        identity = PlanIdentity(key="a" * 12, plan_name="session-deadbeef", plan_ref=None)

        assert identity.is_session_fallback is True
        assert (
            PlanIdentity(key="a" * 12, plan_name="x", plan_ref=Path("/r/x.md")).is_session_fallback
            is False
        )
