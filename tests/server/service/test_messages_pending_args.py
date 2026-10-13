"""Per-session pending tool-args storage tests (server/service/messages.py).

``_pending_args`` / ``_pending_raw`` must be keyed by BARE session id (same
convention as the answering flag at ``state_register_mem.set_state(session_id,
...)``) so concurrent sessions on separate WS connections never see each
other's pending tool state. These tests exercise the same access paths the
``async_generate`` / ``resume_agent`` stream loops use:

- write   — ``_accumulate_pending_args`` (streamed JSON fragments / dict)
- read    — ``_get_pending_args``   (tool_start frame)
- consume — ``_pop_pending_args``   (tool_result frame, updates mode)
- cleanup — ``_clear_pending_args`` (turn-end, finally block; scoped to the
            owning session — other sessions' entries must survive)
"""

from __future__ import annotations

import asyncio

import pytest

from server.service.stream_dispatch import (
    _accumulate_pending_args,
    _clear_pending_args,
    _get_pending_args,
    _pending_args,
    _pending_raw,
    _pop_pending_args,
)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_pending_state():
    """Isolate the module-level pending stashes between tests."""
    _pending_args.clear()
    _pending_raw.clear()
    yield
    _pending_args.clear()
    _pending_raw.clear()


# ────────────────────────────────────────────────────────────────────────────
# concurrency / isolation
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_sessions_isolated():
    """Two sessions accumulating the SAME tool_id concurrently must not see
    each other's data — session A's pending context must not leak into
    session B's turn."""

    async def accumulate_for(session_id: str, value: str):
        # Mirror the streamed-fragment access path: partial JSON, a yield to
        # interleave the two writers, then the closing fragment.
        _accumulate_pending_args(session_id, "tool-1", '{"v":')
        await asyncio.sleep(0)
        _accumulate_pending_args(session_id, "tool-1", f'"{value}"}}')

    await asyncio.gather(accumulate_for("sess-A", "A"), accumulate_for("sess-B", "B"))

    assert _get_pending_args("sess-A", "tool-1") == {"v": "A"}
    assert _get_pending_args("sess-B", "tool-1") == {"v": "B"}


def test_pop_pending_args_per_session():
    """tool_result pop must consume only the owning session's entry — the
    other session's entry for the same tool_call_id survives."""
    _accumulate_pending_args("sess-A", "tool-1", '{"v": "A"}')
    _accumulate_pending_args("sess-B", "tool-1", '{"v": "B"}')

    assert _pop_pending_args("sess-A", "tool-1") == {"v": "A"}
    assert _pop_pending_args("sess-B", "tool-1") == {"v": "B"}
    # Consumed entries are gone (old `.pop(tool_id, {})` default preserved).
    assert _pop_pending_args("sess-A", "tool-1") == {}
    assert _pop_pending_args("sess-B", "tool-1") == {}


def test_get_pop_missing_session_or_tool_returns_empty():
    """Absent session/tool keys degrade to {} — mirrors the old
    ``_pending_args.get(tool_id or "", {})`` / ``.pop(tool_id, {})`` contract
    so the stream loops' wire shapes are unchanged."""
    assert _get_pending_args("sess-unknown", "tool-x") == {}
    assert _get_pending_args("sess-unknown", None) == {}
    assert _pop_pending_args("sess-unknown", "tool-x") == {}


# ────────────────────────────────────────────────────────────────────────────
# scoped turn-end cleanup (the former global `.clear()`)
# ────────────────────────────────────────────────────────────────────────────


def test_clear_scoped_to_session():
    """Turn-end cleanup removes ONLY the owning session's entries; other
    sessions' pending args survive (previously a process-global .clear())."""
    _accumulate_pending_args("sess-A", "tool-1", '{"v": "A"}')
    _accumulate_pending_args("sess-B", "tool-2", '{"v": "B"}')

    _clear_pending_args("sess-A")

    assert _get_pending_args("sess-A", "tool-1") == {}
    assert _get_pending_args("sess-B", "tool-2") == {"v": "B"}
    # Scoped clear must not resurrect anything for the cleared session.
    _clear_pending_args("sess-A")
    assert _get_pending_args("sess-A", "tool-1") == {}


# ────────────────────────────────────────────────────────────────────────────
# content-shape preservation (single-session behavior is unchanged)
# ────────────────────────────────────────────────────────────────────────────


def test_accumulate_semantics_unchanged():
    """Data CONTENT structure is preserved: str fragments accumulate until the
    buffer parses to a non-empty dict; a dict fragment is authoritative and
    resets the raw buffer; None tool_id and non-dict/non-str values ignored."""

    # None tool_id: ignored.
    _accumulate_pending_args("sess-A", None, '{"v": "x"}')
    assert _get_pending_args("sess-A", "") == {}

    # Non-dict/non-str scalar: ignored.
    _accumulate_pending_args("sess-A", "tool-1", 42)
    assert _get_pending_args("sess-A", "tool-1") == {}

    # Partial JSON fragments accumulate; unparseable buffer stays empty.
    _accumulate_pending_args("sess-A", "tool-1", '{"path":')
    assert _get_pending_args("sess-A", "tool-1") == {}

    # Buffer completes -> parsed dict lands in the bag.
    _accumulate_pending_args("sess-A", "tool-1", '"/tmp/f.txt"}')
    assert _get_pending_args("sess-A", "tool-1") == {"path": "/tmp/f.txt"}

    # A dict fragment is authoritative and resets the raw buffer.
    _accumulate_pending_args("sess-A", "tool-1", {"path": "/tmp/g.txt"})
    assert _get_pending_args("sess-A", "tool-1") == {"path": "/tmp/g.txt"}

    # Incomplete JSON *after* a complete dict must NOT clobber it (a complete
    # dict always supersedes an earlier partial string).
    _accumulate_pending_args("sess-A", "tool-1", '{"path":')
    assert _get_pending_args("sess-A", "tool-1") == {"path": "/tmp/g.txt"}
