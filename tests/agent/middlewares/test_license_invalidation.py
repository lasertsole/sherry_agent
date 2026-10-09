"""Compression invalidates the read licenses of the reads it discards.

The read-before-write license (``agent/tools/pub_base/read_state.py``) records
that a session has read a file — but a compression is exactly the moment that
stops being true: the ``read_file`` ToolMessage is replaced by a summary that
names a path and nothing else, while the license (a historical fact) would
survive and let ``write_file`` blind-overwrite from a stale memory. Both
compression paths drop the licenses of the reads they summarize away, with the
preserved tail's later reads kept (the license tracks the LATEST read).

Covers ``_invalidate_compressed_read_licenses`` through the real under-lock
pipeline: the reads go through the real ``read_file`` tool and the consequence
is asserted through the real ``write_file`` / ``patch_file`` tools.
"""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.summarization.core as summarization_module
from agent.middlewares.summarization.summary_doc import SummaryDoc
from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils, read_state
from runtime import state_register_mem

pytestmark = [pytest.mark.module]

SESSION = "s-license-invalidation"


class _RunnableStub:
    def __init__(self, result):
        self.result = result
        self.prompts: list[str] = []

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return self.result

    async def ainvoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return self.result


class _StubModel:
    _llm_type = "fake"

    def __init__(self, result=None):
        self.runnable = _RunnableStub(result or SummaryDoc(goal="g"))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        return self.runnable

    def invoke(self, prompt, config=None):
        return SimpleNamespace(text="")


def _make_middleware() -> summarization_module.Summarization:
    return summarization_module.Summarization(model=_StubModel(), trigger=[("tokens", 10**9)])


def _request(messages, session_id):
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=_StubModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _disable_nudges(monkeypatch):
    monkeypatch.setattr(summarization_module, "_schedule_compression_nudges", lambda *a: None)
    monkeypatch.setattr(summarization_module, "_schedule_compression_todo_update", lambda *a: None)


def _read_call(call_id: str, path: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "read_file", "args": {"file_path": path}, "id": call_id}]
    )


def _read_result(call_id: str, text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id)


def _compress_sync(messages: list, session_id: str, cutoff: int, monkeypatch):
    _disable_nudges(monkeypatch)
    middleware = _make_middleware()
    monkeypatch.setattr(middleware, "_determine_cutoff", lambda msgs: cutoff)
    return middleware._apply_compression_under_lock(_request(messages, session_id), session_id)


async def _compress_async(messages: list, session_id: str, cutoff: int, monkeypatch):
    _disable_nudges(monkeypatch)
    middleware = _make_middleware()
    monkeypatch.setattr(middleware, "_determine_cutoff", lambda msgs: cutoff)
    return await middleware._aapply_compression_under_lock(
        _request(messages, session_id), session_id
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A session root at tmp_path, with the process license registry emptied."""
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))
    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


@pytest.fixture
def sid(request):
    session = "lic-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield session
    read_state.forget_session(session)
    state_register_mem.clear_session(session)


def _read(session: str, path: str, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=session, **kwargs))


def _write(session: str, path: str, text: str, **kwargs) -> str:
    return build_write_file_tool()._core(path, text, session_id=session, **kwargs)


def _patch(session: str, path: str, old: str, new: str) -> dict:
    return json.loads(
        build_patch_file_tool()._core(path, old_string=old, new_string=new, session_id=session)
    )


def _license(session: str, project: Path, name: str):
    return read_state.licensed(session, project / name)


# ---------------------------------------------------------------------------
# The license goes when the read it came from is summarized away
# ---------------------------------------------------------------------------


def test_read_license_invalidated_after_summarization(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")
    assert _license(sid, project, "notes.txt") is not None

    messages = [
        HumanMessage(content="read the notes"),
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="tail question"),
    ]
    _compress_sync(messages, sid, cutoff=3, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is None
    out = json.loads(_write(sid, "notes.txt", "replaced\n"))
    assert "has not been read" in out["error"]
    assert target.read_text(encoding="utf-8") == "original\n"


def test_preserved_read_license_stays_valid(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")

    messages = [
        HumanMessage(content="older turn"),
        HumanMessage(content="read the notes"),
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
    ]
    _compress_sync(messages, sid, cutoff=2, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is not None
    assert "successfully" in _write(sid, "notes.txt", "replaced\n")
    assert target.read_text(encoding="utf-8") == "replaced\n"


def test_multiple_reads_same_file_latest_in_preserved(project, sid, monkeypatch):
    """The license tracks the LATEST read — a later read keeps it alive."""
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")
    _read(sid, "notes.txt")

    messages = [
        HumanMessage(content="turn one"),
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="turn three"),
        _read_call("c2", "notes.txt"),
        _read_result("c2", "original\n"),
    ]
    # Cutoff between the two reads: c1 is summarized, c2 stays visible.
    _compress_sync(messages, sid, cutoff=3, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is not None
    assert "successfully" in _write(sid, "notes.txt", "replaced\n")


def test_multiple_reads_same_file_latest_in_summarized(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")
    _read(sid, "notes.txt")

    messages = [
        HumanMessage(content="turn one"),
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="turn three"),
        _read_call("c2", "notes.txt"),
        _read_result("c2", "original\n"),
        HumanMessage(content="tail question"),
    ]
    # Cutoff past both reads: neither copy of the content survives.
    _compress_sync(messages, sid, cutoff=6, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is None
    out = json.loads(_write(sid, "notes.txt", "replaced\n"))
    assert "has not been read" in out["error"]


def test_multiple_files_partial_overlap(project, sid, monkeypatch):
    """A − B: only the file whose read was summarized away loses its license."""
    (project / "a.txt").write_text("A\n", encoding="utf-8")
    (project / "b.txt").write_text("B\n", encoding="utf-8")
    _read(sid, "a.txt")
    _read(sid, "b.txt")

    messages = [
        _read_call("c1", "a.txt"),
        _read_result("c1", "A\n"),
        _read_call("c2", "b.txt"),
        _read_result("c2", "B\n"),
        _read_call("c3", "b.txt"),
        _read_result("c3", "B\n"),
    ]
    # a.txt and the first b.txt read are summarized; the second b.txt read stays.
    _compress_sync(messages, sid, cutoff=4, monkeypatch=monkeypatch)

    assert _license(sid, project, "a.txt") is None
    assert _license(sid, project, "b.txt") is not None
    assert "has not been read" in json.loads(_write(sid, "a.txt", "A2\n"))["error"]
    assert "successfully" in _write(sid, "b.txt", "B2\n")


# ---------------------------------------------------------------------------
# The neighbours are untouched
# ---------------------------------------------------------------------------


def test_patch_file_unaffected_by_license_invalidation(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    _read(sid, "notes.txt")

    messages = [
        _read_call("c1", "notes.txt"),
        _read_result("c1", "alpha beta\n"),
        HumanMessage(content="tail"),
    ]
    _compress_sync(messages, sid, cutoff=2, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is None
    # patch_file matches old_string text instead of citing a license.
    assert _patch(sid, "notes.txt", "beta", "gamma")["success"] is True
    assert target.read_text(encoding="utf-8") == "alpha gamma\n"


def test_append_unaffected(project, sid, monkeypatch):
    target = project / "log.txt"
    target.write_text("first\n", encoding="utf-8")
    _read(sid, "log.txt")

    messages = [
        _read_call("c1", "log.txt"),
        _read_result("c1", "first\n"),
        HumanMessage(content="tail"),
    ]
    _compress_sync(messages, sid, cutoff=2, monkeypatch=monkeypatch)

    assert "successfully" in _write(sid, "log.txt", "second\n", append=True)
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"
    # An append must not invent a license for the now-unread file.
    assert _license(sid, project, "log.txt") is None
    assert "has not been read" in json.loads(_write(sid, "log.txt", "replaced\n"))["error"]


def test_re_read_re_establishes_license(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")

    messages = [
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="tail"),
    ]
    _compress_sync(messages, sid, cutoff=2, monkeypatch=monkeypatch)

    assert "has not been read" in json.loads(_write(sid, "notes.txt", "replaced\n"))["error"]

    _read(sid, "notes.txt")
    assert "successfully" in _write(sid, "notes.txt", "replaced\n")
    assert target.read_text(encoding="utf-8") == "replaced\n"


def test_async_compression_invalidates_the_same_way(project, sid, monkeypatch):
    """Both compression paths must drop the license (AGENTS.md pitfall)."""
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")

    messages = [
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="tail"),
    ]
    asyncio.run(_compress_async(messages, sid, cutoff=2, monkeypatch=monkeypatch))

    assert _license(sid, project, "notes.txt") is None
    assert "has not been read" in json.loads(_write(sid, "notes.txt", "replaced\n"))["error"]


# ---------------------------------------------------------------------------
# Failure modes stay fail-open
# ---------------------------------------------------------------------------


def test_path_resolution_failure_is_fail_open(project, sid, monkeypatch):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")

    def exploding(*args, **kwargs):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(path_utils, "resolve_workspace_path", exploding)

    messages = [
        _read_call("c1", "notes.txt"),
        _read_result("c1", "original\n"),
        HumanMessage(content="tail"),
    ]
    _compress_sync(messages, sid, cutoff=2, monkeypatch=monkeypatch)

    # The license survives (old behaviour) and the compression itself completed.
    assert _license(sid, project, "notes.txt") is not None


def test_a_read_of_an_unknown_path_is_skipped(project, sid, monkeypatch):
    """A path that escapes the root cannot resolve — the pair is simply skipped."""
    (project / "notes.txt").write_text("original\n", encoding="utf-8")
    _read(sid, "notes.txt")

    messages = [
        _read_call("c1", "../escape.txt"),
        _read_result("c1", "nope"),
        _read_call("c2", "notes.txt"),
        _read_result("c2", "original\n"),
        HumanMessage(content="tail"),
    ]
    _compress_sync(messages, sid, cutoff=4, monkeypatch=monkeypatch)

    assert _license(sid, project, "notes.txt") is None
