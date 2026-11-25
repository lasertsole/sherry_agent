"""B-class patch #15: aggregate size guard for one parallel tool-call batch.

Each parallel tool result can sit just under ContextEviction's per-call 20K
threshold while the batch sums to megabytes — no per-call hook sees the total
and compression cannot cut it. ``_enforce_batch_size_limit`` is the post-gather
guard: over budget, evict the largest results first (disk offload, then in-memory
clip), skip already-evicted results, preserve order/count, and fail open.

Hermetic: the eviction directory is redirected to a tmp_path and the threshold
is lowered via monkeypatch; no real session/context window is needed.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent import safe_tool_node as stn
from config.features import TOOL_PARALLEL
from pub.func.message import eviction as eviction_module
from pub.func.message.eviction import EVICTION_PREFIX, build_clipped_preview

pytestmark = [pytest.mark.unit]

SID = "batch-guard-session"


#: A disk/clip preview is ~4K chars (head+tail line caps). The aggregate
#: threshold must be comfortably above one preview for the guard to reach it.
THRESHOLD = 50_000


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setitem(TOOL_PARALLEL, "batch_result_threshold_chars", THRESHOLD)


def _tool_msg(size: int, call_id: str) -> ToolMessage:
    # Use a non-excluded tool: read_file/write_file/search_files etc. are on
    # TOOL_RESULT_EVICTION["excluded_tools"] and are never offloaded.
    return ToolMessage(
        content="x" * size,
        tool_call_id=call_id,
        name="terminal",
    )


def _sizes(outputs: list[object]) -> list[int]:
    return [stn._msg_content_size(o) for o in outputs]


# ── no-op paths ──────────────────────────────────────────────────────────────


def test_no_trigger_when_under_threshold():
    outputs = [_tool_msg(100, "1"), _tool_msg(200, "2")]
    assert stn._enforce_batch_size_limit(outputs, SID) == outputs


def test_threshold_zero_disables():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(TOOL_PARALLEL, "batch_result_threshold_chars", 0)
    try:
        outputs = [_tool_msg(5_000, "1"), _tool_msg(5_000, "2")]
        assert stn._enforce_batch_size_limit(outputs, SID) == outputs
    finally:
        monkeypatch.undo()


def test_single_output_skips():
    outputs = [_tool_msg(50_000, "1")]
    assert stn._enforce_batch_size_limit(outputs, SID) == outputs


def test_empty_session_id_skips():
    outputs = [_tool_msg(5_000, "1"), _tool_msg(5_000, "2")]
    assert stn._enforce_batch_size_limit(outputs, "") == outputs


# ── eviction behavior ────────────────────────────────────────────────────────


def test_evicts_largest_first_until_under_threshold():
    outputs = [_tool_msg(20_000, "1"), _tool_msg(40_000, "2"), _tool_msg(30_000, "3")]
    original_ids = [o.tool_call_id for o in outputs]

    result = stn._enforce_batch_size_limit(outputs, SID)

    assert [o.tool_call_id for o in result] == original_ids, "order/count preserved"
    assert sum(_sizes(result)) <= THRESHOLD
    # The largest (40K) result is evicted first; after it alone the batch fits.
    assert result[1].content.startswith(EVICTION_PREFIX)
    assert not result[0].content.startswith(EVICTION_PREFIX), "a 20K result may survive"


def test_many_results_under_per_call_threshold_are_all_evicted():
    # Each result is below the evict_threshold_chars (20K) so per-call
    # ContextEviction never touches them; the batch still sums to ~1.9M.
    # Previews have a ~4K floor, so 100 previews cannot fit a 50K threshold —
    # the guard evicts as many as it can (all 100 here) rather than wedging.
    outputs = [_tool_msg(19_000, f"c{i}") for i in range(100)]
    result = stn._enforce_batch_size_limit(outputs, SID)

    assert len(result) == 100
    assert all(o.content.startswith(EVICTION_PREFIX) for o in result)
    assert sum(_sizes(result)) < sum(_sizes(outputs)) / 3, "batch shrinks by >3x"


def test_already_evicted_results_are_skipped():
    pre_evicted = ToolMessage(
        content=f"{EVICTION_PREFIX}/some/path.txt]\npreview",
        tool_call_id="0",
        name="terminal",
    )
    original_content = pre_evicted.content
    outputs = [pre_evicted, _tool_msg(40_000, "1"), _tool_msg(40_000, "2")]

    result = stn._enforce_batch_size_limit(outputs, SID)

    assert result[0].content == original_content, "already-evicted result untouched"
    assert sum(_sizes(result)) <= THRESHOLD


def test_command_wrapped_tool_message_is_evicted():
    inner = _tool_msg(40_000, "1")
    wrapped = Command(update={"messages": [inner]})
    outputs = [wrapped, _tool_msg(40_000, "2")]

    result = stn._enforce_batch_size_limit(outputs, SID)

    assert isinstance(result[0], Command)
    rewritten = result[0].update["messages"][0]
    assert rewritten.content.startswith(EVICTION_PREFIX)
    assert sum(stn._msg_content_size(o) for o in result) <= THRESHOLD


def test_fallback_to_hard_clip_when_offload_fails(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(stn, "evict_tool_result", _boom)
    outputs = [_tool_msg(40_000, "1"), _tool_msg(40_000, "2")]

    result = stn._enforce_batch_size_limit(outputs, SID)

    # One clipped result (~4K) brings 80K under the 50K threshold, so only the
    # largest is clipped; the point is the in-memory fallback actually ran.
    assert any(o.content.startswith("[output truncated: ") for o in result)
    assert sum(_sizes(result)) <= THRESHOLD


def test_fail_open_when_both_eviction_paths_fail(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(stn, "evict_tool_result", _boom)
    monkeypatch.setattr(stn, "hard_clip_tool_result", _boom)
    outputs = [_tool_msg(5_000, "1"), _tool_msg(5_000, "2")]
    original_content = [o.content for o in outputs]

    result = stn._enforce_batch_size_limit(outputs, SID)

    assert [o.content for o in result] == original_content, "results kept, no exception"


# ── force parameter on the eviction primitives ──────────────────────────────


def test_force_skips_per_result_threshold():
    from config.features import TOOL_RESULT_EVICTION

    under = TOOL_RESULT_EVICTION["evict_threshold_chars"] - 100
    msg = _tool_msg(under, "under")

    assert eviction_module.evict_tool_result(msg, SID) is None  # default: under threshold
    assert eviction_module.evict_tool_result(msg, SID, force=True) is not None


def test_force_skips_per_result_threshold_on_clip():
    from config.features import TOOL_RESULT_EVICTION

    under = TOOL_RESULT_EVICTION["evict_threshold_chars"] - 100
    msg = _tool_msg(under, "under")

    assert eviction_module.hard_clip_tool_result(msg) is None
    clipped = eviction_module.hard_clip_tool_result(msg, force=True)
    assert clipped is not None and clipped.content == build_clipped_preview(msg.content)
