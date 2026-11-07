"""read_file oversized-file guards: schema blacklists, whole-read refusal, streaming.

Three layers protect the process heap, in the order a call reaches them:

* the input SCHEMA refuses binary extensions (``.exe``, ``.zip``, ``.db`` …) and
  blocking device paths (``/dev/zero`` …) before any fd is opened;
* a WHOLE-READ REFUSAL rejects a big file when the caller did not ask for a
  range (offset=1 + the default limit) — a range read is never refused;
* files at or above the fast-path threshold are scanned in chunks: only the
  requested window (plus one clipped line head) is retained.

The last file read also never licenses a write (a paged view of a huge file is
not proof the whole file was seen).
"""

from __future__ import annotations

import json
import time
import tracemalloc

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import ValidationError

from agent.tools.file_tools import read_file as read_file_module
from agent.tools.file_tools.read_file import ReadFileInput, build_read_file_tool
from agent.tools.pub_base import path_utils, read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-read-oversized"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


@pytest.fixture
def small_thresholds(monkeypatch):
    """Stream anything ≥64 bytes; refuse whole reads ≥64 bytes.

    The production numbers are pinned by ``test_shipped_thresholds``; the
    behaviour tests do not need 10 MiB fixtures.
    """
    monkeypatch.setattr(read_file_module, "_MAX_WHOLE_READ", 64)
    monkeypatch.setattr(read_file_module, "_FAST_PATH_THRESHOLD", 64)
    monkeypatch.setattr(read_file_module, "_STREAM_CHUNK", 1024)


def _read(path, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(str(path), session_id=SESSION, **kwargs))


def _lines_file(root, count: int, width: int = 4):
    path = root / "big.log"
    path.write_text(
        "".join(f"line{i:0{width}d}\n" for i in range(count)), encoding="utf-8"
    )
    return path


# ── schema-level blacklists ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["setup.exe", "bundle.zip", "store.db", "x.sqlite3", "a.pyc"])
def test_binary_extensions_are_refused_by_the_schema(name):
    with pytest.raises(ValidationError, match="binary"):
        ReadFileInput(file_path=name)


@pytest.mark.parametrize("name", ["/dev/zero", "/dev/urandom", "/dev/tty", "/dev/fd/0"])
def test_blocking_device_paths_are_refused_by_the_schema(name):
    with pytest.raises(ValidationError, match="device file"):
        ReadFileInput(file_path=name)


def test_an_uppercase_binary_extension_is_refused_too():
    with pytest.raises(ValidationError, match=r"binary \.exe"):
        ReadFileInput(file_path="C:\\tools\\SETUP.EXE")


def test_a_dotfile_is_not_mistaken_for_a_binary_extension():
    assert ReadFileInput(file_path=".gitignore").file_path == ".gitignore"


# ── whole-read refusal ─────────────────────────────────────────────────────


def test_an_oversized_whole_read_is_refused_without_opening_the_file(
    project, small_thresholds, monkeypatch
):
    path = _lines_file(project, 20)

    def _must_not_read(*_args, **_kwargs):
        raise AssertionError("the whole-read refusal must not read any byte")

    monkeypatch.setattr(read_file_module.ReadFileTool, "_read_bytes", _must_not_read)

    result = _read(path)

    assert "whole-read limit" in result["error"]
    assert result["file_size"] == path.stat().st_size
    assert "offset=1&limit=" in result["hint"]
    assert "content" not in result


def test_an_explicit_default_limit_is_still_a_whole_read(project, small_thresholds):
    path = _lines_file(project, 20)

    result = _read(path, limit=read_file_module._READ_DEFAULT_LIMIT)

    assert "whole-read limit" in result["error"]


def test_a_range_read_of_the_same_file_is_allowed(project, small_thresholds):
    path = _lines_file(project, 20)

    result = _read(path, offset=1, limit=5)

    assert "error" not in result
    assert result["oversized"] is True
    # _lines_file numbers from 0, so the first five lines are 1..5 = line0000..4.
    assert result["content"].splitlines() == [f"{i + 1}|line{i:04d}" for i in range(5)]


# ── streaming window ───────────────────────────────────────────────────────


def test_the_streaming_window_reports_an_exact_total(project, small_thresholds):
    path = _lines_file(project, 20)

    result = _read(path, offset=5, limit=3)

    assert result["total_lines"] == 20
    assert result["content"].splitlines() == ["5|line0004", "6|line0005", "7|line0006"]
    assert result["truncated"] is True
    assert result["truncated_by_token_cap"] is False
    assert "offset=8" in result["hint"]


def test_an_offset_beyond_the_end_is_an_empty_page(project, small_thresholds):
    path = _lines_file(project, 8)

    result = _read(path, offset=100, limit=5)

    assert result["content"] == ""
    assert result["total_lines"] == 8
    assert result["truncated"] is True


def test_total_lines_matches_splitlines_without_a_trailing_newline(project, small_thresholds):
    path = project / "no-trailing.txt"
    path.write_text("a\nb\nc", encoding="utf-8")

    result = _read(path, offset=1, limit=10)

    assert result["total_lines"] == 3
    assert result["content"] == "1|a\n2|b\n3|c"


def test_a_streamed_read_grants_no_write_license(project, small_thresholds):
    path = _lines_file(project, 20)

    _read(path, offset=1, limit=5)

    assert read_state.licensed(SESSION, path) is None


def test_disabling_both_thresholds_restores_the_historical_path(project, monkeypatch):
    monkeypatch.setattr(read_file_module, "_MAX_WHOLE_READ", 0)
    monkeypatch.setattr(read_file_module, "_FAST_PATH_THRESHOLD", 0)
    path = project / "small.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")

    result = _read(path)

    assert result["content"] == "1|one\n2|two"
    assert result["truncated"] is False
    assert "oversized" not in result
    assert read_state.licensed(SESSION, path) is not None


# ── encodings and boundaries ───────────────────────────────────────────────


def test_a_utf8_character_split_across_a_chunk_boundary_is_decoded(project, small_thresholds):
    # 1023 ASCII bytes + one 3-byte CJK char: the 1024-byte chunk ends inside it.
    path = project / "boundary.txt"
    path.write_text("a" * 1023 + "你\nsecond\n", encoding="utf-8")

    result = _read(path, offset=1, limit=2)

    assert result["content"].splitlines()[0] == "1|" + "a" * 1023 + "你"
    assert result["total_lines"] == 2


def test_a_utf16_file_streams_with_its_own_codec(project, small_thresholds):
    path = project / "utf16.txt"
    path.write_bytes("alpha\nbeta\ngamma\n".encode("utf-16"))

    result = _read(path, offset=1, limit=2)

    assert result["content"] == "1|alpha\n2|beta"
    assert result["total_lines"] == 3


def test_crlf_lines_are_normalized_by_the_streaming_reader(project, small_thresholds):
    # Above the (monkeypatched) streaming threshold, so the reader's own
    # normalizer runs; the whole-file path keeps "\r" (pre-existing shape).
    path = project / "crlf.txt"
    path.write_bytes("".join(f"row{i}\r\n" for i in range(20)).encode("utf-8"))

    result = _read(path, offset=1, limit=2)

    assert result["content"] == "1|row0\n2|row1"
    assert result["total_lines"] == 20


def test_a_binary_file_without_a_known_extension_is_refused_by_the_sniff(project, small_thresholds):
    path = project / "blob"
    path.write_bytes(b"\x00\x01\x02" * 40)

    result = _read(path, offset=1, limit=5)

    assert "binary or not UTF-8/UTF-16" in result["error"]
    assert "content" not in result


# ── clipped single line + token-cap flag ───────────────────────────────────


def test_a_single_huge_line_is_clipped_and_bounded(project, small_thresholds):
    path = project / "minified.js"
    path.write_text("x" * 400_000, encoding="utf-8")

    result = _read(path, offset=1, limit=10)

    assert "chars clipped" in result["content"]
    # The page is bounded by the token budget, not by the 400 KB line.
    assert len(result["content"]) < 40_000
    assert result["total_lines"] == 1


def test_the_token_cap_flag_is_set_when_the_budget_ends_the_page(project, small_thresholds, monkeypatch):
    monkeypatch.setattr(read_file_module, "_TOKEN_BUDGET", 50)
    path = project / "wide.log"
    path.write_text("\n".join("y" * 100 for _ in range(10)) + "\n", encoding="utf-8")

    result = _read(path, offset=1, limit=10)

    assert result["truncated_by_token_cap"] is True
    assert len(result["content"].splitlines()) < 10


def test_the_token_cap_flag_stays_off_when_the_line_window_ends_the_page(
    project, small_thresholds, monkeypatch
):
    monkeypatch.setattr(read_file_module, "_TOKEN_BUDGET", 4000)
    path = _lines_file(project, 20)

    result = _read(path, offset=1, limit=3)

    assert result["truncated_by_token_cap"] is False
    assert len(result["content"].splitlines()) == 3


# ── memory + line-token short-circuit ─────────────────────────────────────


def test_streaming_a_large_file_keeps_peak_memory_below_the_file(project, small_thresholds):
    path = project / "memory.log"
    chunk = ("z" * 200 + "\n").encode("utf-8")
    with path.open("wb") as fh:
        for _ in range(10_000):  # ~2 MB
            fh.write(chunk)

    tracemalloc.start()
    try:
        result = _read(path, offset=1, limit=5)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["total_lines"] == 10_000
    assert peak < path.stat().st_size * 0.6, (peak, path.stat().st_size)


def test_line_token_cost_short_circuits_on_a_giant_line():
    giant = "x" * 2_000_000

    started = time.monotonic()
    cost = read_file_module._line_token_cost(giant, cap=10.0)
    elapsed = time.monotonic() - started

    assert cost > 10.0
    assert elapsed < 0.2, elapsed


def test_line_token_cost_without_a_cap_is_unchanged():
    assert read_file_module._line_token_cost("hello") == pytest.approx(1.25)
    assert read_file_module._line_token_cost("你好") == pytest.approx(1.0)


# ── shipped thresholds ────────────────────────────────────────────────────


def test_shipped_thresholds():
    from config.features import TOOLS_TIMEOUTS

    assert TOOLS_TIMEOUTS["read_file_max_whole_read_bytes"] == 262_144
    assert TOOLS_TIMEOUTS["read_file_fast_path_threshold_bytes"] == 10_485_760
    assert TOOLS_TIMEOUTS["read_file_stream_chunk_bytes"] == 65_536


# ── tool-level end-to-end (LangChain validation + handle_tool_error) ──────


def test_the_tool_surface_raises_the_binary_refusal(project):
    """The schema gate runs before any fd; a direct invoke raises it."""
    tool = build_read_file_tool()

    with pytest.raises(ValidationError, match="binary"):
        tool.invoke({"file_path": "setup.exe", "session_id": SESSION})


class _ToolCallingModel(BaseChatModel):
    """First turn: call read_file on setup.exe; after the tool: answer."""

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        if any(getattr(message, "type", "") == "tool" for message in messages):
            answer = AIMessage(content="done")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "setup.exe"},
                        "id": "call-1",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_the_agent_graph_turns_the_schema_refusal_into_an_error_tool_message(project):
    """End-to-end through the REAL agent loop (the schema gate inside ToolNode).

    ``handle_tool_error`` covers ``_run`` failures; an args-schema rejection is
    raised before that, and the node wraps it into a ``ToolInvocationError``
    whose message becomes the model-visible error ToolMessage.
    """
    from langchain.agents import create_agent
    from langchain_core.messages import ToolMessage

    graph = create_agent(model=_ToolCallingModel(), tools=[build_read_file_tool()])
    out = graph.invoke({"messages": [("human", "read setup.exe")]})

    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages, out["messages"]
    assert tool_messages[-1].status == "error"
    assert "binary" in str(tool_messages[-1].content)


def test_the_tool_surface_refuses_a_whole_read_of_an_oversized_file(project, small_thresholds):
    path = _lines_file(project, 20)

    result = build_read_file_tool().invoke({"file_path": str(path), "session_id": SESSION})

    payload = json.loads(result) if isinstance(result, str) else json.loads(str(result.content))
    assert "whole-read limit" in payload["error"]
