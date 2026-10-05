"""Stage 4 backend: the revert state endpoint, the revert endpoint, the frames.

The client's chip is driven by ``file_changes_updated`` (pushed after a write and
answered by the ``file_changes_refresh`` frame), and the button posts to
``/sessions/file-changes/revert`` — which calls the very same core the
agent-side tool does, so the two entry points can never drift.
"""

import asyncio
import json

import pytest

from agent.tools.file_tools import snapshot as snapshot_mod
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools import snapshot_push
from agent.tools.file_tools.snapshot_push import FILE_CHANGES_UPDATED_EVENT, push_file_changes
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils
from server.service.file_changes_service import file_changes_state, revert_session_file_changes

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-file-changes-api"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    yield root


def _write_and_read(path: str, text: str, call: str) -> None:
    target = path_utils.ROOT_DIR / path
    if not target.exists():
        target.write_text("original\n", encoding="utf-8")
    build_read_file_tool()._core(path, session_id=SESSION)
    build_write_file_tool()._core(path, text, session_id=SESSION, tool_call_id=call)


def test_the_state_payload_lists_what_can_be_reverted(project):
    _write_and_read("a.txt", "changed\n", "call-1")

    state = file_changes_state(SESSION)

    assert state["total_rows"] == 1
    assert state["canRevert"] is True
    assert state["changes"][0]["tool_call_id"] == "call-1"


def test_the_service_reverts_through_the_same_core(project, monkeypatch):
    _write_and_read("a.txt", "changed\n", "call-1")
    noticed: list[tuple[str, list[str]]] = []

    async def _capture_notice(session_id: str, paths: list[str]) -> bool:
        noticed.append((session_id, paths))
        return True

    monkeypatch.setattr("agent.tools.file_tools.revert.append_revert_notice", _capture_notice)

    result = asyncio.run(revert_session_file_changes(SESSION))

    assert result["success"] is True
    assert (path_utils.ROOT_DIR / "a.txt").read_text(encoding="utf-8") == "original\n"
    assert file_changes_state(SESSION)["canRevert"] is False
    # The agent is told what was undone — the conversation itself is untouched.
    assert noticed == [(SESSION, [str(path_utils.ROOT_DIR / "a.txt")])]


def test_the_service_honours_dry_run(project):
    _write_and_read("a.txt", "changed\n", "call-1")

    result = asyncio.run(revert_session_file_changes(SESSION, dry_run=True))

    assert result["dry_run"] is True
    assert (path_utils.ROOT_DIR / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_a_push_without_a_socket_is_silent(project):
    """Best-effort by contract: no client attached, no exception, no frame."""
    asyncio.run(push_file_changes(SESSION))
    asyncio.run(push_file_changes(""))


def test_a_push_sends_the_documented_frame(project, monkeypatch):
    _write_and_read("a.txt", "changed\n", "call-1")
    sent: list[str] = []

    class _Socket:
        async def send_text(self, payload: str) -> None:
            sent.append(payload)

    monkeypatch.setattr(
        snapshot_push.relation_register, "get_websocket_by_session_id", lambda sid: _Socket()
    )

    asyncio.run(push_file_changes(SESSION))

    assert sent
    frame = json.loads(sent[0])
    assert frame["event"] == FILE_CHANGES_UPDATED_EVENT
    assert frame["session_id"] == SESSION
    assert frame["content"]["canRevert"] is True


def test_a_broken_socket_never_raises(project, monkeypatch):
    class _DeadSocket:
        async def send_text(self, payload: str) -> None:
            raise RuntimeError("socket closed")

    monkeypatch.setattr(
        snapshot_push.relation_register, "get_websocket_by_session_id", lambda sid: _DeadSocket()
    )

    asyncio.run(push_file_changes(SESSION))  # must not raise


def test_the_refresh_processor_answers_the_same_shape(project):
    from server.trigger.core import ws_event_processor_dict

    _write_and_read("a.txt", "changed\n", "call-1")
    processor = ws_event_processor_dict["file_changes_refresh"]

    reply = asyncio.run(processor(SESSION, ""))

    assert reply is not None
    assert reply["event"] == FILE_CHANGES_UPDATED_EVENT
    assert reply["content"] == file_changes_state(SESSION)


def test_disabled_snapshots_report_nothing_to_revert(project, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "enabled", False)
    _write_and_read("a.txt", "changed\n", "call-1")

    state = file_changes_state(SESSION)

    assert state["canRevert"] is False
    assert snapshot_mod.rows_for_session(SESSION) == []
