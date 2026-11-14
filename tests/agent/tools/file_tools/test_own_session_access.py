"""Own-session file access at the TOOL boundary.

A session (main OR child) may read, write, modify and delete every file under
its own ``SESSIONS_DIR/<session_id>`` tree without a HITL approval — the files
are its private runtime state (plans, attachments, uploads, ``evicted/``
previews, scratch files). Another session's tree stays out of reach:

* the main agent's read of a foreign session still raises the external-path
  HITL card (this file pins that the OWN bypass did not open it up), and
* a subagent's foreign read is refused outright (it cannot self-approve).

The session root here lives OUTSIDE the project directory, which is the case
the explicit ``resolve_external_path`` bypass exists for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

MAIN = "sess-own"
CHILD = "agent:main:session:sess-own:sub:1"
OTHER = "sess-other"


class _InterruptRecorder:
    """Stand-in for langgraph's ``interrupt`` (records, then rejects)."""

    def __init__(self) -> None:
        self.prompts: list[dict] = []

    def __call__(self, value):
        self.prompts.append(value)
        return {"decisions": [{"type": "reject", "message": "not mine"}]}


@pytest.fixture()
def own_tree(tmp_path: Path, monkeypatch):
    """Point SESSIONS_DIR at a temp tree and create the session directories."""
    from runtime import state_register_mem
    from runtime.session.state_keys import StateKey

    sessions = tmp_path / "sessions"
    monkeypatch.setattr("config.path.SESSIONS_DIR", sessions)
    # Orthogonal gates that a previous suite may have left armed in the shared
    # runtime state: without this, a leftover YOLO flag would silently allow the
    # cross-session reads below instead of exercising the HITL/subagent branch.
    monkeypatch.setattr("agent.tools.pub_base.path_utils._is_yolo", lambda: False)
    monkeypatch.setattr("agent.tools.pub_base.path_utils._check_allowlist", lambda *_a, **_k: False)
    state_register_mem.clear_session(MAIN)
    for session in (MAIN, CHILD, OTHER):
        (sessions / session / "evicted").mkdir(parents=True, exist_ok=True)
    (sessions / MAIN / "evicted" / "big.txt").write_text("hello own session\n")
    (sessions / CHILD / "scratch.txt").write_text("child private\n")
    (sessions / OTHER / "secret.txt").write_text("someone else\n")
    # The external gate recognizes the child by its caller scope (the same key
    # spawn writes); without it the "child cannot self-approve" branch is not
    # exercised.
    state_register_mem.set_state(CHILD, StateKey.CALLER_SCOPE, "subagent")
    yield sessions
    state_register_mem.clear_session(CHILD)


@pytest.fixture()
def no_snapshots(monkeypatch):
    """Skip shadow-git snapshots (unrelated to path policy, slow on a checkout)."""
    for module in ("write_file", "patch_file"):
        monkeypatch.setattr(
            f"agent.tools.file_tools.{module}.ensure_checkpoint", lambda *_a, **_k: None
        )


def _assert_ok(out: str) -> dict:
    payload = json.loads(out)
    assert "error" not in payload, payload
    return payload


class TestOwnSessionIsNotExternal:
    def test_main_reads_its_own_tree_without_hitl(self, own_tree: Path, monkeypatch):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)

        out = build_read_file_tool().invoke(
            {"file_path": str(own_tree / MAIN / "evicted" / "big.txt"), "session_id": MAIN}
        )

        assert "hello own session" in out
        assert recorder.prompts == []

    def test_child_reads_its_own_tree_without_hitl(self, own_tree: Path, monkeypatch):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)

        out = build_read_file_tool().invoke(
            {"file_path": str(own_tree / CHILD / "scratch.txt"), "session_id": CHILD}
        )

        assert "child private" in out
        assert recorder.prompts == []

    def test_main_writes_and_creates_directories_without_hitl(
        self, own_tree: Path, no_snapshots, monkeypatch
    ):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)
        target = own_tree / MAIN / "scratch" / "note.txt"

        payload = _assert_ok(
            build_write_file_tool().invoke(
                {"file_path": str(target), "text": "first\n", "session_id": MAIN}
            )
        )

        assert payload["success"] is True
        assert target.read_text() == "first\n"
        assert recorder.prompts == []

    def test_child_modifies_its_own_file_without_hitl(
        self, own_tree: Path, no_snapshots, monkeypatch
    ):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)
        target = own_tree / CHILD / "scratch.txt"

        payload = _assert_ok(
            build_patch_file_tool().invoke(
                {
                    "file_path": str(target),
                    "old_string": "child private",
                    "new_string": "child edited",
                    "session_id": CHILD,
                }
            )
        )

        assert payload["success"] is True
        assert target.read_text() == "child edited\n"
        assert recorder.prompts == []


class TestCrossSessionIsolation:
    def test_main_reading_another_session_still_asks_hitl(self, own_tree: Path, monkeypatch):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)

        out = build_read_file_tool().invoke(
            {"file_path": str(own_tree / OTHER / "secret.txt"), "session_id": MAIN}
        )

        assert "someone else" not in out
        assert "denied" in out.lower()
        assert len(recorder.prompts) == 1

    def test_child_cannot_read_another_session(self, own_tree: Path, monkeypatch):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)

        out = build_read_file_tool().invoke(
            {"file_path": str(own_tree / OTHER / "secret.txt"), "session_id": CHILD}
        )

        assert "someone else" not in out
        assert "not authorized for subagent" in out
        assert recorder.prompts == []

    def test_child_cannot_write_another_session(self, own_tree: Path, no_snapshots, monkeypatch):
        recorder = _InterruptRecorder()
        monkeypatch.setattr("langgraph.types.interrupt", recorder)
        target = own_tree / OTHER / "secret.txt"

        out = build_write_file_tool().invoke(
            {"file_path": str(target), "text": "pwned", "session_id": CHILD}
        )

        assert "error" in json.loads(out)
        assert target.read_text() == "someone else\n"
        assert recorder.prompts == []
