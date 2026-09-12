"""Unit tests for the LT-2 pending-TaskFlow injection in workspace/prompt_builder.py.

On session start the prompt builder scans the taskflow registry for
non-terminal flows whose ``state['creator_session_key']`` matches the current
session and injects a concise summary (max 3 flows) so the agent can
proactively continue unfinished work. The block must fail-open and is skipped
when the caller filters to explicit files, matching the memory-block rule.
"""

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self):
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id, key, default=None):
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id, key, value):
        self._states.setdefault(session_id, {})[key] = value
        return True


class FakeMemoryStore:
    """Stand-in for agent.tools.memory.memory_store."""

    def format_for_system_prompt(self, target: str):
        return "MEMORY-LIVE" if target == "memory" else None


class FakeTieredStore:
    """Stand-in for TieredMemoryStore (empty listing -> no FACTS block)."""

    def get_facts_listing(self) -> str:
        return ""


class FakeFlowStore:
    """Stand-in for taskflow.registry.store_sqlite with a controllable result."""

    def __init__(self):
        self.flows: list[dict] = []
        self.error: Exception | None = None
        self.calls = 0

    def get_active_flows_sync(self) -> list[dict]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.flows


pytestmark = [pytest.mark.unit]


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """Isolate persona files, memory, todos, boulder, facts and the flow store."""
    file_names = ["AGENTS.md", "SOUL.md"]
    (tmp_path / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-PERSONA", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", file_names)
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())
    monkeypatch.setattr("agent.tools.memory.memory_store", FakeMemoryStore())
    monkeypatch.setattr("agent.tools.memory_tiered.get_tiered_store", lambda: FakeTieredStore())

    # No boulder file and no todos -> those blocks stay empty.
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", tmp_path / "boulder.json")
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync", lambda session_id: []
    )

    flow_store = FakeFlowStore()
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_active_flows_sync",
        flow_store.get_active_flows_sync,
    )

    return {"flow_store": flow_store}


def _step(step_id, task, status="done"):
    return {"step_id": step_id, "task": task, "depends_on": [], "status": status}


def _flow(
    flow_id,
    *,
    status="running",
    session_id="sess-main",
    description="do work",
    steps=None,
    creator_key=None,
):
    """Build a registry flow row. ``creator_key=None`` + ``session_id`` -> match."""
    state: dict = {"description": description, "steps": steps or []}
    if session_id is not None:
        state["creator_session_key"] = creator_key or f"agent:main:session:{session_id}"
    return {"flow_id": flow_id, "status": status, "state": state}


def _steps_progress(*statuses):
    return [
        _step(f"s{index}", f"task {index}", status=status)
        for index, status in enumerate(statuses, start=1)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTaskflowBlockInjection:
    def test_taskflow_block_injected_when_active(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [_flow("deploy-v2")]

        prompt = build_system_prompt(session_id="sess-main")

        assert "## Pending TaskFlows" in prompt
        assert "deploy-v2" in prompt
        assert "Use taskflow_summary to inspect a flow and continue execution." in prompt

    def test_taskflow_block_absent_when_no_flows(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = []

        prompt = build_system_prompt(session_id="sess-main")

        assert "Pending TaskFlows" not in prompt
        assert "AGENTS-PERSONA" in prompt

    def test_taskflow_block_absent_when_session_mismatch(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [
            _flow("other-flow", creator_key="agent:main:session:someone-else")
        ]

        prompt = build_system_prompt(session_id="sess-main")

        assert "Pending TaskFlows" not in prompt
        assert prompt_env["flow_store"].calls == 1

    def test_taskflow_block_absent_when_no_session(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [_flow("deploy-v2")]

        prompt = build_system_prompt(session_id=None)

        assert "Pending TaskFlows" not in prompt
        assert prompt_env["flow_store"].calls == 0

    def test_taskflow_block_shows_step_progress(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [
            _flow(
                "deploy-v2",
                steps=_steps_progress("done", "done", "ready", "blocked", "dispatched"),
            )
        ]

        prompt = build_system_prompt(session_id="sess-main")

        assert "2/5 steps done" in prompt
        assert 'next: s3 "task 3"' in prompt

    def test_taskflow_block_max_three_flows(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [
            _flow(f"flow-{index}", description=f"work {index}") for index in range(1, 6)
        ]

        prompt = build_system_prompt(session_id="sess-main")

        assert "flow-1" in prompt
        assert "flow-2" in prompt
        assert "flow-3" in prompt
        assert "flow-4" not in prompt
        assert "flow-5" not in prompt


class TestTaskflowFailOpen:
    def test_taskflow_block_fail_open(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].error = RuntimeError("registry db unavailable")

        prompt = build_system_prompt(session_id="sess-main")

        assert "Pending TaskFlows" not in prompt
        assert "AGENTS-PERSONA" in prompt
        assert "SKILLS-BLOCK" in prompt


class TestTaskflowGuards:
    def test_taskflow_block_absent_when_filtered_files(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [_flow("deploy-v2")]

        prompt = build_system_prompt(selected_file_names=["AGENTS.md"], session_id="sess-main")

        assert "Pending TaskFlows" not in prompt
        assert prompt_env["flow_store"].calls == 0
        assert "AGENTS-PERSONA" in prompt

    def test_old_flow_without_creator_key_not_injected(self, prompt_env):
        from workspace.prompt_builder import build_system_prompt

        prompt_env["flow_store"].flows = [_flow("legacy-flow", session_id=None)]

        prompt = build_system_prompt(session_id="sess-main")

        assert "Pending TaskFlows" not in prompt
        assert "legacy-flow" not in prompt
