"""Task 5 TDD suite: 4-route overflow routing integrated into the Summarization
middleware T1 (before_agent PREFLIGHT) / T2 (wrap_model_call PRE-API) paths,
plus the two anti-thrash state keys (cooldown + per-turn attempt cap).

QA scenarios implemented as tests (plan .omo/plans/context-compression.md
L588-628):
1. soft overflow -> truncate track (NO auxiliary-LLM compression call)
2. cooldown suppresses consecutive triggers (2nd skipped, later call recovers)
3. low pressure -> complete no-op (byte-identical, no compression state writes)
4. sync/async parity (identical message transforms)

Harness style (StubModel / ModelRequest / sid fixture) copied from
tests/module/test_summarization_comprehensive.py. New module-level names are
accessed ONLY at runtime via mget() (module-getattr pattern) so collection
does not break before implementation.
"""

import asyncio
import contextlib
import uuid

import pytest
from loguru import logger
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.agents.middleware import ModelRequest

import agent.middlewares.summarization as summarization_module
from runtime import state_register_mem

# ----------------------------------------------------------------------
# Window math (Task 5 contract): dynamic window is the constructor-injected
# main_llm_context_window (same source as agent/core.py:156); usable budget
# subtracts COMPRESSION_RESERVE_TOKENS. No hardcoded 65536 anywhere.
# ----------------------------------------------------------------------
CTX_WINDOW = 41600
COMPRESSION_RESERVE_TOKENS = 16000
USABLE_BUDGET = CTX_WINDOW - COMPRESSION_RESERVE_TOKENS  # 25600
# threshold_truncate = USABLE * 0.70 = 17920; threshold_compact = 25600 * 0.80 = 20480
TRUNCATE_BUDGET_TOKENS = int(USABLE_BUDGET * 0.6)  # 15360


# ======================================================================
# Harness (baseline style)
# ======================================================================


class StubModel:
    _llm_type = "fake"

    def __init__(self, text=None, raise_exc=False):
        self.text = text or (
            "The assistant completed the analysis and decided on a final approach for the task at hand."
        )
        self.raise_exc = raise_exc
        self.calls = []

    def invoke(self, prompt, config=None):
        self.calls.append(prompt)
        if self.raise_exc:
            raise RuntimeError("stub llm failure")
        return SimpleNamespace(text=self.text)

    async def ainvoke(self, prompt, config=None):
        self.calls.append(prompt)
        if self.raise_exc:
            raise RuntimeError("stub llm failure")
        return SimpleNamespace(text=self.text)


def make_request(messages, session_id="t5-x", model=None):
    return ModelRequest(
        model=model or StubModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def make_middleware(**overrides):
    kwargs = dict(
        model=overrides.pop("model", StubModel()),
        trigger=overrides.pop("trigger", [("tokens", 80000)]),
        keep=overrides.pop("keep", ("messages", 10)),
        main_llm_context_window=overrides.pop(
            "main_llm_context_window", CTX_WINDOW
        ),
        need_update_system_prompt=overrides.pop("need_update_system_prompt", False),
    )
    kwargs.update(overrides)
    return summarization_module.Summarization(**kwargs)


def mget(name):
    return getattr(summarization_module, name)


@pytest.fixture
def sid(request):
    s = "t5-" + request.node.name[:40] + "-" + uuid.uuid4().hex[:6]
    yield s
    try:
        state_register_mem.clear_session(s)
    except Exception:
        pass


@contextlib.contextmanager
def capture_logs(level="INFO"):
    lines = []
    handler_id = logger.add(lines.append, level=level, format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


# ======================================================================
# Message builders
# ======================================================================


def _ai_with_call(tc_id, name="search", args=None):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args or {"q": "x"}, "id": tc_id}]
    )


def soft_overflow_messages():
    """Pressure in [0.70, 0.80) * usable with one big ToolMessage candidate.

    est(msg[2]) = (76000 + 10) // 4 = 19002; total ~19006 in
    [17920, 20480) → soft overflow with a candidate → truncate route.
    9 messages so index 2 < keep_until (9 - 6).
    """
    return [
        HumanMessage(content="q1"),
        _ai_with_call("t5-call-1"),
        ToolMessage(content="x" * 76000, tool_call_id="t5-call-1"),
        HumanMessage(content="q2"),
        AIMessage(content="a2"),
        HumanMessage(content="q3"),
        AIMessage(content="a3"),
        HumanMessage(content="q4"),
        AIMessage(content="a4"),
    ]


def hard_overflow_messages():
    """est 88000 // 4 = 22000 >= 20480 (threshold_compact); NO ToolMessage
    candidates → compact_only route."""
    return [HumanMessage(content="h" * 88000)]


def low_pressure_messages():
    return [HumanMessage(content="hello"), AIMessage(content="hi there")]


TTL_MARKER = "truncated by context compression"


# ======================================================================
# QA scenario 1: T2 soft overflow routes to the truncate track
# ======================================================================


class TestT2SoftOverflow:
    def test_t2_soft_overflow_routes_to_truncate_track(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        msgs = soft_overflow_messages()
        req = make_request(msgs, session_id=sid, model=stub)
        captured = {}

        def handler(r):
            captured["messages"] = list(r.messages)
            return AIMessage(content="ok")

        with capture_logs() as lines:
            resp = mw.wrap_model_call(req, handler)

        # auxiliary LLM must NOT be called on the truncate track
        assert stub.calls == []
        assert resp.content == "ok"
        # message list length unchanged (in-place truncation, no deletion)
        assert len(captured["messages"]) == 9
        tool = captured["messages"][2]
        # big ToolMessage truncated (Task 4 head/tail + non-empty placeholder)
        assert len(tool.content) < 76000
        assert TTL_MARKER in tool.content
        # untouched neighbours keep their identity/content
        assert captured["messages"][0].content == "q1"
        assert captured["messages"][8].content == "a4"
        # route log present with trigger label
        assert any("trigger=T2" in line for line in lines)
        assert any("route=truncate_tool_results_only" in line for line in lines)
        assert any("old_tokens=" in line and "new_tokens=" in line for line in lines)
        assert any("pressure_ratio=" in line for line in lines)
        # NO compression happened: compression count key untouched
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) in (None, 0)

    def test_t2_soft_overflow_recheck_passes_below_compact_threshold(self, sid):
        """After truncation pressure < threshold_compact → no compact backstop."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        req = make_request(soft_overflow_messages(), session_id=sid, model=stub)
        with capture_logs() as lines:
            mw.wrap_model_call(req, lambda r: AIMessage(content="ok"))
        # exactly ONE route action logged (no compact backstop after recheck)
        assert sum(1 for line in lines if "route=" in line) == 1
        assert not any("route=compact" in line for line in lines)


# ======================================================================
# QA scenario 2: cooldown suppresses consecutive triggers
# ======================================================================


class TestT2Cooldown:
    def test_cooldown_suppresses_then_recovers(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        big = hard_overflow_messages()
        small = low_pressure_messages()

        def handler(r):
            return AIMessage(content="ok")

        cooldown_key = mget("_COOLDOWN_ROUNDS_KEY")  # AttributeError-RED on old module
        count_key = mget("_COMPRESSION_COUNT_KEY")

        # 1st call: actual compression happens, cooldown set to 3
        with capture_logs():
            mw.wrap_model_call(make_request(big, sid, stub), handler)
        assert state_register_mem.get_state(sid, count_key) == 1
        assert state_register_mem.get_state(sid, cooldown_key) == 3

        # 2nd call (equal pressure): suppressed by cooldown, decremented 3→2
        with capture_logs() as lines:
            mw.wrap_model_call(make_request(big, sid, stub), handler)
        assert state_register_mem.get_state(sid, count_key) == 1
        assert state_register_mem.get_state(sid, cooldown_key) == 2
        assert not any("route=compact_only" in line for line in lines)

        # 3 low-pressure calls tick the cooldown down 2→1→0
        for _ in range(3):
            with capture_logs():
                mw.wrap_model_call(make_request(small, sid, stub), handler)
        assert state_register_mem.get_state(sid, cooldown_key) == 0

        # next equal-pressure call: cooldown exhausted → compression recovers
        with capture_logs():
            mw.wrap_model_call(make_request(big, sid, stub), handler)
        assert state_register_mem.get_state(sid, count_key) == 2
        assert state_register_mem.get_state(sid, cooldown_key) == 3

    def test_turn_attempt_cap_stops_proactive_compression(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        attempts_key = mget("_TURN_ATTEMPTS_KEY")  # AttributeError-RED on old module
        count_key = mget("_COMPRESSION_COUNT_KEY")
        # simulate 3 compressions already counted this turn
        state_register_mem.set_state(sid, attempts_key, 3)
        state_register_mem.set_state(sid, count_key, 3)

        def handler(r):
            return AIMessage(content="ok")

        with capture_logs() as lines:
            mw.wrap_model_call(
                make_request(hard_overflow_messages(), sid, stub), handler
            )
        # cap reached → proactive compression stopped for the rest of the turn
        assert state_register_mem.get_state(sid, count_key) == 3
        assert state_register_mem.get_state(sid, attempts_key) == 3
        assert not any("route=compact" in line for line in lines)


# ======================================================================
# QA scenario 3: low pressure → complete no-op
# ======================================================================


class TestT2NegativeNoop:
    def test_low_pressure_complete_noop(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        msgs = low_pressure_messages()
        req = make_request(msgs, session_id=sid, model=stub)
        captured = {}

        def handler(r):
            captured["r"] = r
            return AIMessage(content="ok")

        with capture_logs() as lines:
            resp = mw.wrap_model_call(req, handler)

        # byte-identical passthrough (same contract as the trigger test)
        assert resp.content == "ok"
        assert captured["r"] is req
        assert [m.content for m in req.messages] == ["hello", "hi there"]
        assert stub.calls == []
        # no trigger log, no compression state writes
        assert not any("trigger=" in line for line in lines)
        for key in (
            mget("_COOLDOWN_ROUNDS_KEY"),
            mget("_TURN_ATTEMPTS_KEY"),
            mget("_COMPRESSION_COUNT_KEY"),
            mget("_COMPRESSION_INEFFECTIVE_KEY"),
        ):
            assert state_register_mem.get_state(sid, key) in (None, 0)


# ======================================================================
# QA scenario 4: sync and async paths are equivalent
# ======================================================================


class TestSyncAsyncParity:
    def test_wrap_and_awrap_identical_transforms(self, sid):
        sid2 = sid + "-async"
        stub1, stub2 = StubModel(), StubModel()
        mw1 = make_middleware(model=stub1)
        mw2 = make_middleware(model=stub2)
        sync_messages = soft_overflow_messages()
        async_messages = soft_overflow_messages()
        captured_sync = []
        captured_async = []

        def handler(r):
            captured_sync.extend(r.messages)
            return AIMessage(content="ok")

        async def ahandler(r):
            captured_async.extend(r.messages)
            return AIMessage(content="ok")

        with capture_logs():
            mw1.wrap_model_call(make_request(sync_messages, sid, stub1), handler)
        with capture_logs():
            asyncio.run(
                mw2.awrap_model_call(
                    make_request(async_messages, sid2, stub2), ahandler
                )
            )

        try:
            assert len(captured_sync) == len(captured_async) == 9
            assert [m.content for m in captured_sync] == [
                m.content for m in captured_async
            ]
            assert TTL_MARKER in captured_sync[2].content
            assert TTL_MARKER in captured_async[2].content
            assert stub1.calls == [] and stub2.calls == []
        finally:
            try:
                state_register_mem.clear_session(sid2)
            except Exception:
                pass


# ======================================================================
# T1 (before_agent PREFLIGHT)
# ======================================================================


class TestT1Preflight:
    def test_t1_soft_overflow_truncates_via_state_update(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        msgs = soft_overflow_messages()

        with capture_logs() as lines:
            result = mw.before_agent({"session_id": sid, "messages": msgs}, None)

        assert isinstance(result, dict)
        new_msgs = result["messages"]
        # same-id replacement list: length unchanged, big ToolMessage truncated
        assert len(new_msgs) == 9
        assert TTL_MARKER in new_msgs[2].content
        assert len(new_msgs[2].content) < 76000
        assert new_msgs[0].content == "q1"
        assert stub.calls == []
        assert any("trigger=T1" in line for line in lines)
        assert any("route=truncate_tool_results_only" in line for line in lines)

    def test_t1_fits_returns_none(self, sid):
        mw = make_middleware()
        result = mw.before_agent(
            {"session_id": sid, "messages": low_pressure_messages()}, None
        )
        assert result is None

    def test_t1_resets_turn_attempts_per_turn(self, sid):
        mw = make_middleware()
        attempts_key = mget("_TURN_ATTEMPTS_KEY")  # AttributeError-RED on old module
        state_register_mem.set_state(sid, attempts_key, 2)
        mw.before_agent({"session_id": sid, "messages": low_pressure_messages()}, None)
        assert state_register_mem.get_state(sid, attempts_key) == 0

    def test_t1_cooldown_gates_compact_route(self, sid):
        """Cooldown only blocks proactive compression — T1 compact is proactive."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        cooldown_key = mget("_COOLDOWN_ROUNDS_KEY")
        count_key = mget("_COMPRESSION_COUNT_KEY")
        state_register_mem.set_state(sid, cooldown_key, 2)

        result = mw.before_agent(
            {"session_id": sid, "messages": hard_overflow_messages()}, None
        )
        assert result is None
        assert stub.calls == []
        assert state_register_mem.get_state(sid, count_key) in (None, 0)


# ======================================================================
# Route-decision helper (upgraded _preemptive_check decision)
# ======================================================================


class TestRouteDecision:
    def test_decide_overflow_route_four_way(self, sid):
        mw = make_middleware()
        decide = getattr(mw, "_decide_overflow_route")
        # low → fits
        assert decide(low_pressure_messages(), sid) in (None, "fits")
        # soft with candidate → truncate_tool_results_only
        assert decide(soft_overflow_messages(), sid) == "truncate_tool_results_only"
        # hard without candidates → compact_only
        assert decide(hard_overflow_messages(), sid) == "compact_only"

    def test_no_context_window_no_route(self, sid):
        mw = make_middleware(main_llm_context_window=None)
        decide = getattr(mw, "_decide_overflow_route")
        assert decide(soft_overflow_messages(), sid) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
