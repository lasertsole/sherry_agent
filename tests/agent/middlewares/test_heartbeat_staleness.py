"""Unit tests for HeartbeatStaleness — skip_heartbeat bypass and staleness regression."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agent.middlewares.heartbeat_staleness import (
    _STATE_KEY_KILLED,
    _STATE_KEY_SKIP,
    _STATE_KEY_STALE,
    _STATE_KEY_TOOL,
    HeartbeatStaleness,
    HeartbeatTimeoutError,
)
from runtime import state_register_mem

pytestmark = [pytest.mark.unit]


SKIP_TOOL = SimpleNamespace(metadata={"skip_heartbeat": True})
PLAIN_TOOL = SimpleNamespace(metadata={})
NO_METADATA_TOOL = SimpleNamespace()


def _req(sess: str, name: str = "question", tool: object = SKIP_TOOL) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "c1"},
        tool=tool,
        state={"session_id": sess},
        runtime=None,
    )


def _tight_mw() -> HeartbeatStaleness:
    return HeartbeatStaleness(stale_cycles_idle=2, stale_cycles_in_tool=3)


# ============================================================================
# wrap_tool_call: skip flag vs normal tracking
# ============================================================================


class TestWrapToolCall:
    def test_skip_heartbeat_tool_sets_skip_not_tool(self):
        mw = HeartbeatStaleness()
        sess = "s-wrap-skip"

        assert mw._wrap_tool_call_impl(_req(sess)) is None
        assert state_register_mem.get_state(sess, _STATE_KEY_SKIP, False) is True
        assert state_register_mem.has_key(sess, _STATE_KEY_TOOL) is False

    def test_normal_tool_tracked_not_skipped(self):
        mw = HeartbeatStaleness()
        sess = "s-wrap-normal"

        assert mw._wrap_tool_call_impl(_req(sess, name="terminal", tool=PLAIN_TOOL)) is None
        assert state_register_mem.get_state(sess, _STATE_KEY_TOOL, None) == "terminal"
        assert state_register_mem.has_key(sess, _STATE_KEY_SKIP) is False

    def test_tool_without_metadata_still_tracked(self):
        mw = HeartbeatStaleness()
        sess = "s-wrap-nometa"

        mw._wrap_tool_call_impl(_req(sess, name="web_search", tool=NO_METADATA_TOOL))
        assert state_register_mem.get_state(sess, _STATE_KEY_TOOL, None) == "web_search"

    def test_wrap_raises_when_killed(self):
        mw = HeartbeatStaleness()
        sess = "s-wrap-killed"
        state_register_mem.set_state(sess, _STATE_KEY_KILLED, True)

        with pytest.raises(HeartbeatTimeoutError):
            mw._wrap_tool_call_impl(_req(sess))


# ============================================================================
# _check_progress: skip while suspended, stale-kill regression
# ============================================================================


class TestCheckProgress:
    def test_progress_check_skips_counting_while_skip_set(self):
        mw = _tight_mw()
        sess = "s-progress-skip"
        state_register_mem.set_state(sess, _STATE_KEY_SKIP, True)

        for _ in range(6):
            mw._check_progress(sess)

        assert state_register_mem.get_state(sess, _STATE_KEY_STALE, 0) == 0
        assert state_register_mem.get_state(sess, _STATE_KEY_KILLED, False) is False

    def test_progress_check_regression_stale_kill_still_fires(self):
        mw = _tight_mw()
        sess = "s-progress-stale"
        state_register_mem.set_state(sess, _STATE_KEY_TOOL, "terminal")

        for _ in range(4):
            mw._check_progress(sess)

        assert state_register_mem.get_state(sess, _STATE_KEY_KILLED, False) is True

    def test_progress_check_regression_idle_kill_still_fires(self):
        mw = _tight_mw()
        sess = "s-progress-idle"

        for _ in range(3):
            mw._check_progress(sess)

        assert state_register_mem.get_state(sess, _STATE_KEY_KILLED, False) is True

    def test_progress_check_counts_again_after_skip_cleared(self):
        mw = _tight_mw()
        sess = "s-progress-resume"
        state_register_mem.set_state(sess, _STATE_KEY_SKIP, True)
        mw._check_progress(sess)

        state_register_mem.set_state(sess, _STATE_KEY_SKIP, False)
        for _ in range(3):
            mw._check_progress(sess)

        assert state_register_mem.get_state(sess, _STATE_KEY_KILLED, False) is True


# ============================================================================
# after_tool_call: clears skip alongside tool key
# ============================================================================


class TestAfterToolCall:
    def test_after_tool_clears_skip_and_tool(self):
        mw = HeartbeatStaleness()
        sess = "s-after"
        mw._wrap_tool_call_impl(_req(sess))
        assert state_register_mem.get_state(sess, _STATE_KEY_SKIP, False) is True

        mw._after_tool_call_impl(_req(sess))

        assert state_register_mem.get_state(sess, _STATE_KEY_SKIP, False) is False
        assert state_register_mem.get_state(sess, _STATE_KEY_TOOL, "unset") is None


# ============================================================================
# async path flows through the same impl
# ============================================================================


class TestAsyncPath:
    @pytest.mark.asyncio
    async def test_awrap_tool_call_honors_skip_flag(self):
        mw = HeartbeatStaleness()
        sess = "s-async"
        seen: dict[str, object] = {}

        async def handler(request: ToolCallRequest) -> ToolMessage:
            seen["skip"] = state_register_mem.get_state(sess, _STATE_KEY_SKIP, False)
            seen["tool_key_set"] = state_register_mem.has_key(sess, _STATE_KEY_TOOL)
            return ToolMessage(content="ok", tool_call_id="c1", name="question")

        out = await mw.awrap_tool_call(_req(sess), handler)

        assert out.content == "ok"
        assert seen["skip"] is True
        assert seen["tool_key_set"] is False
        assert state_register_mem.get_state(sess, _STATE_KEY_SKIP, False) is False
