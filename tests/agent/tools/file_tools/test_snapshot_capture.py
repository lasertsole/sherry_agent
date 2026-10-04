"""Stage 1 acceptance: every file-tool write leaves one snapshot row behind.

The contract: capture happens inside the write lock, from bytes the call
already holds (or one read for ``write_file``), and the row records what the
revert needs — the old content (a content-addressed blob), the revision the
write produced, and the tool call it belongs to. Nothing here may fail a write:
a broken store degrades to a warning.
"""

import asyncio
import hashlib
import json
import sqlite3

import pytest

from agent.tools.file_tools import snapshot as snapshot_mod
from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.snapshot import (
    capture_pre_write,
    rows_for_session,
    snapshot_store,
)
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-snapshot-capture"


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


def _write(session: str, path: str, text: str, **kwargs) -> str:
    return build_write_file_tool()._core(path, text, session_id=session, **kwargs)


def _read(session: str, path: str) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=session))


def test_a_write_to_an_existing_file_keeps_the_old_bytes(project):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")

    _read(SESSION, "notes.txt")
    _write(SESSION, "notes.txt", "rewritten\n", tool_call_id="call-1")

    rows = rows_for_session(SESSION)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_call_id == "call-1"
    assert row.existed_before is True
    assert row.before_revision != "absent"
    # The blob holds exactly the content the write replaced.
    assert snapshot_mod.blob_bytes(SESSION, row.blob_sha256) == b"original\n"
    # after_sha256 describes the file as the write left it.
    assert row.after_sha256 == hashlib.sha256(b"rewritten\n").hexdigest()
    assert target.read_text(encoding="utf-8") == "rewritten\n"


def test_creating_a_file_records_no_blob(project):
    _write(SESSION, "fresh.txt", "hello\n", tool_call_id="call-new")

    rows = rows_for_session(SESSION)
    assert len(rows) == 1
    assert rows[0].existed_before is False
    assert rows[0].blob_sha256 is None


def test_three_writes_leave_three_rows_and_deduplicate_blobs(project):
    target = project / "loop.txt"
    target.write_text("v0\n", encoding="utf-8")

    _read(SESSION, "loop.txt")
    _write(SESSION, "loop.txt", "v1\n", tool_call_id="call-a")
    _write(SESSION, "loop.txt", "v2\n", tool_call_id="call-b")
    _write(SESSION, "loop.txt", "v3\n", tool_call_id="call-c")

    rows = rows_for_session(SESSION)
    assert [r.tool_call_id for r in rows] == ["call-a", "call-b", "call-c"]
    # Three distinct old contents: v0, v1, v2 — all stored, none repeated.
    blobs = [r.blob_sha256 for r in rows]
    assert len(set(blobs)) == 3

    # Rewriting one of those old contents again reuses its blob.
    target.write_text("v1\n", encoding="utf-8")
    _read(SESSION, "loop.txt")
    _write(SESSION, "loop.txt", "v9\n", tool_call_id="call-d")

    rows = rows_for_session(SESSION)
    assert rows[-1].blob_sha256 == blobs[1]


def test_an_append_snapshots_the_pre_append_content(project):
    target = project / "log.txt"
    target.write_text("line-1\n", encoding="utf-8")

    _write(SESSION, "log.txt", "line-2\n", append=True, tool_call_id="call-append")

    rows = rows_for_session(SESSION)
    assert len(rows) == 1
    assert snapshot_mod.blob_bytes(SESSION, rows[0].blob_sha256) == b"line-1\n"
    # after_sha256 describes the WHOLE file as the append left it.
    assert rows[0].after_sha256 == hashlib.sha256(b"line-1\nline-2\n").hexdigest()


def test_a_patch_reads_once_and_snapshots_the_patched_content(project, monkeypatch):
    target = project / "code.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

    import agent.tools.file_tools.patch_file as patch_mod

    real_read = patch_mod.read_bytes_no_follow
    reads = {"count": 0}

    def counting_read(path):
        reads["count"] += 1
        return real_read(path)

    monkeypatch.setattr(patch_mod, "read_bytes_no_follow", counting_read)

    result = json.loads(
        build_patch_file_tool()._core(
            "code.py",
            old_string="beta = 2",
            new_string="beta = 3",
            session_id=SESSION,
            tool_call_id="call-patch",
        )
    )

    assert result["success"] is True
    # Two reads belong to the CAS (read + re-check); the snapshot adds none.
    assert reads["count"] == 2
    rows = rows_for_session(SESSION)
    assert len(rows) == 1
    assert rows[0].tool_call_id == "call-patch"
    assert snapshot_mod.blob_bytes(SESSION, rows[0].blob_sha256) == b"alpha = 1\nbeta = 2\n"


def test_a_refused_write_never_snapshots(project):
    """No write landed, so there is nothing to revert."""
    target = project / "keep.txt"
    target.write_text("content\n", encoding="utf-8")

    out = json.loads(_write(SESSION, "keep.txt", "clobber"))

    assert "has not been read" in out["error"]
    assert rows_for_session(SESSION) == []


def test_a_broken_index_leaves_the_write_working(project, monkeypatch):
    """Fail-open: the snapshot is an enhancement, never a gate."""
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(SESSION, "notes.txt")

    from loguru import logger

    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(message), level="WARNING")

    def boom(**_fields):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(snapshot_store, "insert_row", boom)
    try:
        out = _write(SESSION, "notes.txt", "rewritten\n", tool_call_id="call-x")
    finally:
        logger.remove(sink)

    assert "successfully" in out
    assert target.read_text(encoding="utf-8") == "rewritten\n"
    assert any("snapshot record failed" in message for message in captured), captured


def test_the_snapshot_reads_the_old_bytes_only_once(project, monkeypatch):
    """write_file reads the target once for capture; the write itself re-reads nothing."""
    target = project / "once.txt"
    target.write_text("old\n", encoding="utf-8")
    _read(SESSION, "once.txt")

    import agent.tools.file_tools.snapshot as snap_mod

    real_read = snap_mod.read_bytes_no_follow
    calls = {"n": 0}

    def counting_read(path):
        calls["n"] += 1
        return real_read(path)

    monkeypatch.setattr(snap_mod, "read_bytes_no_follow", counting_read)

    _write(SESSION, "once.txt", "new\n", tool_call_id="call-once")

    assert calls["n"] == 1


def test_the_real_graph_injects_the_tool_call_id(project):
    """R3 gate: the annotated id reaches the tool through the real agent graph."""
    from langchain.agents import AgentState, create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class ToolFakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001
            return self

    class State(AgentState):
        """The production state shape in miniature (see ``StateSchema``)."""

        session_id: str

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "graph.txt", "text": "from the graph\n"},
                    "id": "call-graph-42",
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    agent = create_agent(
        ToolFakeModel(messages=iter(calls)),
        tools=[build_write_file_tool()],
        state_schema=State,
    )
    # The tool reads its session from the graph STATE (InjectedState), which is
    # where a real turn carries it too.
    asyncio.run(
        agent.ainvoke(
            {"messages": [{"role": "user", "content": "go"}], "session_id": SESSION},
            config={"configurable": {"session_id": SESSION}},
        )
    )

    rows = rows_for_session(SESSION)
    assert [r.tool_call_id for r in rows] == ["call-graph-42"]


def test_capture_reports_absent_without_reading(project, monkeypatch):
    """A create pays no read at all."""
    import agent.tools.file_tools.snapshot as snap_mod

    def no_read(_path):
        raise AssertionError("a missing target must not be read")

    monkeypatch.setattr(snap_mod, "read_bytes_no_follow", no_read)
    capture = capture_pre_write(project / "missing.txt", project)

    assert capture is not None
    assert capture.existed_before is False
    assert capture.before_bytes is None


def test_disabled_config_keeps_the_old_behavior(project, monkeypatch):
    from config.features import FILE_SNAPSHOT

    monkeypatch.setitem(FILE_SNAPSHOT, "enabled", False)
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")

    _read(SESSION, "notes.txt")
    assert "successfully" in _write(SESSION, "notes.txt", "rewritten\n", tool_call_id="c")

    assert rows_for_session(SESSION) == []
    assert target.read_text(encoding="utf-8") == "rewritten\n"


def test_payload_groups_rows_per_tool_call(project):
    target = project / "a.txt"
    target.write_text("a0\n", encoding="utf-8")
    _read(SESSION, "a.txt")
    _write(SESSION, "a.txt", "a1\n", tool_call_id="call-1")

    other = project / "b.txt"
    other.write_text("b0\n", encoding="utf-8")
    _read(SESSION, "b.txt")
    _write(SESSION, "b.txt", "b1\n", tool_call_id="call-2")

    payload = snapshot_mod.file_changes_payload(SESSION)

    assert payload["total_rows"] == 2
    assert payload["canRevert"] is True
    assert [change["tool_call_id"] for change in payload["changes"]] == ["call-1", "call-2"]
    assert payload["changes"][0]["paths"] == [str(target)]
