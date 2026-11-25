"""Cross-turn diff-hash stagnation detection (product-side no_progress_stop).

Complements the plan-text stagnation check: a model can keep editing the TODO
text while the actual code patch stays byte-identical. The tracker fingerprints
the working-tree diff at each continuation turn end; an unchanged patch for
``diff_stagnation_threshold`` consecutive turns — with no step child in flight
— stops the continuation gate instead of burning more turns.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.todo_continuation.core as tc
from agent.tools.todolist import stagnation_tracker as st
from agent.tools.todolist.registry import store_sqlite as todo_store
from runtime import hooks

pytestmark = [pytest.mark.unit]

_SID = "sess-diff"
_NO_PROGRESS_MARKER = "[SYSTEM DIRECTIVE: NO PROGRESS DETECTED]"
_REAL_DIFF_HASH = tc._working_tree_diff_hash  # restore under the autouse stub


@pytest.fixture(autouse=True)
def _clean() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


# -- tracker unit ------------------------------------------------------------


def test_empty_hash_is_fail_open_and_clears_streak():
    assert st.check_diff_stagnation(_SID, "h1") is False
    assert st.check_diff_stagnation(_SID, "h1") is False  # count=1, below threshold
    assert st.check_diff_stagnation(_SID, "") is False  # fail-open, clears
    assert st.check_diff_stagnation(_SID, "h1") is False  # fresh baseline again


def test_identical_hash_hits_threshold():
    assert st.check_diff_stagnation(_SID, "abc") is False
    assert st.check_diff_stagnation(_SID, "abc") is False
    assert st.check_diff_stagnation(_SID, "abc") is True  # 2 consecutive matches


def test_changed_hash_resets_streak():
    assert st.check_diff_stagnation(_SID, "abc") is False
    assert st.check_diff_stagnation(_SID, "abc") is False
    assert st.check_diff_stagnation(_SID, "def") is False  # progress -> reset to 0
    assert st.check_diff_stagnation(_SID, "def") is False
    assert st.check_diff_stagnation(_SID, "def") is True


def test_record_baseline_resets_streak():
    st.check_diff_stagnation(_SID, "abc")
    st.check_diff_stagnation(_SID, "abc")  # count=1
    st.record_diff_baseline(_SID, "abc")  # new phase -> count=0
    assert st.check_diff_stagnation(_SID, "abc") is False
    assert st.check_diff_stagnation(_SID, "abc") is True


def test_full_reset_clears_diff_state():
    st.check_diff_stagnation(_SID, "abc")
    st.check_diff_stagnation(_SID, "abc")
    st.reset(_SID)
    assert st.check_diff_stagnation(_SID, "abc") is False


def test_threshold_derived_from_config():
    from config.features import TODOLIST_INFRA

    assert st._DIFF_STAGNATION_THRESHOLD == TODOLIST_INFRA["diff_stagnation_threshold"]


# -- _working_tree_diff_hash -------------------------------------------------


def test_diff_hash_fail_open_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_working_tree_diff_hash", _REAL_DIFF_HASH)
    assert tc._working_tree_diff_hash(tmp_path) == ""


def test_diff_hash_changes_when_a_tracked_file_changes(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(tc, "_working_tree_diff_hash", _REAL_DIFF_HASH)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("alpha")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    before = tc._working_tree_diff_hash(tmp_path)
    (tmp_path / "f.txt").write_text("beta")
    after = tc._working_tree_diff_hash(tmp_path)
    assert before and after and before != after


# -- middleware integration --------------------------------------------------


def _todo(content="build api", status="pending"):
    return {
        "content": content,
        "status": status,
        "priority": "high",
        "flow_id": None,
        "step_id": None,
    }


def _state(continuation: bool = True):
    state = {"session_id": _SID}
    if continuation:
        state["messages"] = [
            HumanMessage(
                content="[SYSTEM DIRECTIVE: TODO CONTINUATION] keep going",
                metadata={"origin": "todo_continuation", "internal": True},
            )
        ]
    return state


@pytest.fixture()
def spy_auto_turn() -> Iterator[list[HumanMessage]]:
    calls: list[HumanMessage] = []

    async def _spy(session_key: str, injection: HumanMessage) -> object:
        calls.append(injection)
        return object()

    hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _spy)
    yield calls
    hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(todo_store, "get_todos_sync", lambda sid: [_todo()])
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda sid: [])
    monkeypatch.setattr(tc, "_has_in_flight_steps", lambda flows: False)
    monkeypatch.setattr(tc, "_working_tree_diff_hash", lambda project_dir: "same-hash")


@pytest.mark.asyncio
async def test_unchanged_patch_stops_after_threshold(spy_auto_turn):
    enforcer = tc.PlanContinuationEnforcer()
    # First two continuation turns: continuation prompt, streak building.
    await enforcer.aafter_agent(_state())
    await enforcer.aafter_agent(_state())
    assert all(_NO_PROGRESS_MARKER not in m.content for m in spy_auto_turn)

    # Third continuation turn: diff streak hits threshold -> no-progress stop.
    await enforcer.aafter_agent(_state())

    assert any(_NO_PROGRESS_MARKER in m.content for m in spy_auto_turn)
    assert st.is_stopped(_SID) is True


@pytest.mark.asyncio
async def test_changed_patch_never_stops(spy_auto_turn, monkeypatch):
    counter = {"n": 0}

    def _hash(_project_dir):
        counter["n"] += 1
        return f"hash-{counter['n']}"

    monkeypatch.setattr(tc, "_working_tree_diff_hash", _hash)
    enforcer = tc.PlanContinuationEnforcer()
    for _ in range(6):
        await enforcer.aafter_agent(_state())

    assert spy_auto_turn  # continuation prompts still flowing
    assert all(_NO_PROGRESS_MARKER not in m.content for m in spy_auto_turn)


@pytest.mark.asyncio
async def test_in_flight_child_suppresses_stop(spy_auto_turn, monkeypatch):
    monkeypatch.setattr(tc, "_has_in_flight_steps", lambda flows: True)
    enforcer = tc.PlanContinuationEnforcer()
    for _ in range(6):
        await enforcer.aafter_agent(_state())

    assert all(_NO_PROGRESS_MARKER not in m.content for m in spy_auto_turn)


@pytest.mark.asyncio
async def test_real_user_turn_rebaselines(spy_auto_turn, monkeypatch):
    enforcer = tc.PlanContinuationEnforcer()
    await enforcer.aafter_agent(_state(continuation=True))
    await enforcer.aafter_agent(_state(continuation=True))
    # A real user turn arrives: streak must reset, not carry over.
    st.mark_injected(_SID)
    await enforcer.aafter_agent(_state(continuation=False))
    before = len(spy_auto_turn)
    await enforcer.aafter_agent(_state(continuation=True))

    assert len(spy_auto_turn) == before + 1
    assert _NO_PROGRESS_MARKER not in spy_auto_turn[-1].content
