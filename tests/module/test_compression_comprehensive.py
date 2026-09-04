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

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langchain.agents.middleware import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)

import agent.middlewares.summarization as summarization_module
from config.num import (
    INEFFECTIVE_THRESHOLD,
    MAX_COMPRESS_ATTEMPTS_PER_TURN,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
    MIN_EFFECTIVENESS_PCT,
    TRUNCATE_BUDGET_RATIO,
    TTL_REGISTRY_MAX_ENTRIES,
)
from pub_func.message.tool_result_ttl import record_first_seen
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
        # full-clear + rebuild update: the RemoveMessage(REMOVE_ALL_MESSAGES)
        # sentinel heads the list, the rebuilt tail carries the truncated
        # big ToolMessage (same-id content swap).
        assert isinstance(new_msgs[0], RemoveMessage)
        assert new_msgs[0].id == REMOVE_ALL_MESSAGES
        rebuilt = list(new_msgs[1:])
        assert len(rebuilt) == 9
        assert TTL_MARKER in rebuilt[2].content
        assert len(rebuilt[2].content) < 76000
        assert rebuilt[0].content == "q1"
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


# ======================================================================
# Task 6 / T3: post-response real-token re-check
# ======================================================================

T3_TRIGGER_REPORTED = 30000  # >= 20480 (threshold_compact); wins over est 11750
T3_TRUNCATE_REPORTED = 22000  # hard zone, overflow <= 0 + candidates -> truncate
T3_BELOW_REPORTED = 10000  # < 20480 -> below threshold


def _ai_with_usage(content, input_tokens):
    return AIMessage(
        content=content,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 100,
            "total_tokens": input_tokens + 100,
        },
    )


def t3_low_est_messages():
    """est = 47000 // 4 = 11750 < 17920 (threshold_truncate) -> T2 complete
    no-op (route fits, token trigger 80000 unreached). Single-big-message
    shape proven to compact (same shape as hard_overflow_messages)."""
    return [HumanMessage(content="h" * 47000)]


def t3_truncate_messages():
    """9-msg shape: ToolMessage est = (66000 + 9) // 4 = 16502 < 17920 -> T2
    no-op; candidate at idx 2 (2 < 9 - 6); reported 22000 in the hard zone
    with overflow <= 0 -> T3 truncate_tool_results_only."""
    return [
        HumanMessage(content="q1"),
        _ai_with_call("t3-call-1"),
        ToolMessage(content="x" * 66000, tool_call_id="t3-call-1"),
        HumanMessage(content="q2"),
        AIMessage(content="a2"),
        HumanMessage(content="q3"),
        AIMessage(content="a3"),
        HumanMessage(content="q4"),
        AIMessage(content="a4"),
    ]


class TestT3Trigger:
    def test_t3_names_exist_on_module(self):
        # RED marker: module-level name via module-getattr; the check methods
        # live on the Summarization class (Task 5 pattern: instance getattr)
        mget("extract_reported_input_tokens")
        mw = make_middleware()
        getattr(mw, "_post_response_check")
        getattr(mw, "_apost_response_check")

    def test_t3_compact_when_t2_did_not_fire(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        req = make_request(t3_low_est_messages(), session_id=sid, model=stub)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(req, lambda r: mr)

        # original response object never lost
        assert resp is mr
        # T2 must NOT have fired (estimate low)
        assert not any("trigger=T2" in line for line in lines)
        # T3 fired: detailed log + route log, both labelled trigger=T3
        assert any(
            "trigger=T3" in line and "reported_input_tokens=30000" in line
            for line in lines
        )
        assert any(
            "trigger=T3" in line and "route=compact_only" in line
            for line in lines
        )
        # actual compact execution bookkeeping
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) == 1
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) == 3
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_t3_truncate_route_hard_zone_with_candidates(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        req = make_request(t3_truncate_messages(), session_id=sid, model=stub)
        captured = {}
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRUNCATE_REPORTED)])

        def handler(r):
            captured["messages"] = list(r.messages)
            return mr

        with capture_logs() as lines:
            resp = mw.wrap_model_call(req, handler)

        assert resp is mr
        assert not any("trigger=T2" in line for line in lines)
        assert any(
            "trigger=T3" in line and "route=truncate_tool_results_only" in line
            for line in lines
        )
        # truncate route ran in place: marker + shrink, NO aux-LLM call
        tool = captured["messages"][2]
        assert TTL_MARKER in tool.content
        assert len(tool.content) < 66000
        assert stub.calls == []
        # truncate-only route does NOT consume a turn attempt
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) in (None, 0)

    def test_t3_async_parity(self, sid):
        sid2 = sid + "-async"
        stub = StubModel()
        mw = make_middleware(model=stub)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        async def ahandler(r):
            return mr

        try:
            with capture_logs() as lines:
                resp = asyncio.run(
                    mw.awrap_model_call(
                        make_request(t3_low_est_messages(), sid2, stub), ahandler
                    )
                )
            assert resp is mr
            assert not any("trigger=T2" in line for line in lines)
            assert any(
                "trigger=T3" in line and "route=compact_only" in line
                for line in lines
            )
            attempts_key = mget("_TURN_ATTEMPTS_KEY")
            assert state_register_mem.get_state(sid2, attempts_key) == 1
        finally:
            try:
                state_register_mem.clear_session(sid2)
            except Exception:
                pass

    def test_apost_response_check_is_real_coroutine(self):
        import inspect

        assert inspect.iscoroutinefunction(
            getattr(make_middleware(), "_apost_response_check")
        )


class TestT3ThreeForms:
    def test_extract_three_forms(self):
        extract = mget("extract_reported_input_tokens")
        ai = _ai_with_usage("ok", 123)
        # bare AIMessage: usage_metadata["input_tokens"]
        assert extract(ai) == 123
        # langchain ModelResponse: probe its message body (.result list)
        assert extract(ModelResponse(result=[ai])) == 123
        tool = ToolMessage(content="t", tool_call_id="c1")
        assert extract(ModelResponse(result=[tool, ai])) == 123
        # duck-typed ModelResponse-like stub
        assert extract(SimpleNamespace(result=[ai])) == 123
        # ExtendedModelResponse: nested unwrap
        ext = ExtendedModelResponse(model_response=ModelResponse(result=[ai]))
        assert extract(ext) == 123
        # permissive unwrap: ExtendedModelResponse wrapping a bare message
        assert extract(ExtendedModelResponse(model_response=ai)) == 123

    def test_extract_none_forms(self):
        extract = mget("extract_reported_input_tokens")
        assert extract(AIMessage(content="no usage")) is None
        # {} / malformed values cannot pass AIMessage pydantic validation —
        # probe the identical getattr path via model_copy (no revalidation)
        # and duck-typed stubs.
        empty = AIMessage(content="x").model_copy(update={"usage_metadata": {}})
        assert extract(empty) is None
        malformed = AIMessage(content="x").model_copy(
            update={
                "usage_metadata": {
                    "input_tokens": "abc",
                    "output_tokens": 1,
                    "total_tokens": 1,
                }
            }
        )
        assert extract(malformed) is None
        null_tokens = AIMessage(content="x").model_copy(
            update={
                "usage_metadata": {
                    "input_tokens": None,
                    "output_tokens": 1,
                    "total_tokens": 1,
                }
            }
        )
        assert extract(null_tokens) is None
        assert extract(SimpleNamespace(usage_metadata={})) is None
        assert extract(SimpleNamespace(usage_metadata={"input_tokens": "abc"})) is None
        assert extract(_ai_with_usage("x", 0)) is None  # degenerate 0 -> None
        assert extract(ModelResponse(result=[HumanMessage(content="p")])) is None
        assert extract(ModelResponse(result=[])) is None
        assert extract("plain string") is None
        assert extract(None) is None
        assert extract(object()) is None
        assert extract(True) is None

    def test_extract_never_raises(self):
        extract = mget("extract_reported_input_tokens")

        class Boom:
            @property
            def usage_metadata(self):
                raise RuntimeError("boom")

        class SelfLoop:
            @property
            def model_response(self):
                return self

        assert extract(Boom()) is None
        assert extract(SelfLoop()) is None


class TestT3NegativeDouble:
    def test_t2_just_compressed_t3_skipped(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        req = make_request(hard_overflow_messages(), session_id=sid, model=stub)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(req, lambda r: mr)

        assert resp is mr
        # exactly ONE trigger record, and it is T2's
        trig = [line for line in lines if "trigger=" in line]
        assert len(trig) == 1
        assert "trigger=T2" in trig[0]
        # T3 detailed log never fired (no reported_input_tokens anywhere)
        assert not any("reported_input_tokens=" in line for line in lines)
        # single compression this turn
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) == 1
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) == 3

    def test_cooldown_active_t3_noop(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        state_register_mem.set_state(sid, mget("_COOLDOWN_ROUNDS_KEY"), 2)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), lambda r: mr
            )

        assert resp is mr
        assert not any("trigger=" in line for line in lines)
        # T3 read the POST-tick value: 2 -> 1 at wrap entry, T3 sees 1 -> noop
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) == 1
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) in (None, 0)

    def test_attempts_exhausted_t3_noop(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        state_register_mem.set_state(sid, attempts_key, 3)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), lambda r: mr
            )

        assert resp is mr
        assert not any("trigger=" in line for line in lines)
        assert state_register_mem.get_state(sid, attempts_key) == 3

    def test_below_threshold_t3_noop(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_BELOW_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), lambda r: mr
            )

        assert resp is mr
        assert not any("trigger=" in line for line in lines)
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) in (None, 0)

    def test_dispatch_failure_response_preserved(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)

        def boom(*args, **kwargs):
            raise RuntimeError("dispatch boom")

        mw._dispatch_overflow_route = boom
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), lambda r: mr
            )

        assert resp is mr  # original response intact despite dispatch failure
        assert not any("route=" in line for line in lines)
        assert any("dispatch boom" in line for line in lines)
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        assert state_register_mem.get_state(sid, attempts_key) in (None, 0)


# ======================================================================
# Task 7 / T4-T5: bounded forced-compression recovery loop
# ======================================================================


class Provider413Error(Exception):
    """413-shaped provider error (classifier channel 1: status_code attr)."""

    status_code = 413


T5_OVERFLOW_TEXT = "This model's maximum context length is 65536 tokens"


class TestT4T5Recovery:
    """Plan QA scenarios (.omo/plans/context-compression.md): T4 recover,
    T5 recover, exhaust->propagate, negative passthrough, sync/async parity,
    plus independent per-class counters and gate/skip bypass coverage.

    Fixture shape (t3_low_est_messages): T2 is a complete no-op on it
    (est 11750 < 17920 threshold_truncate; token trigger 80000 unreached),
    so every compression observed here is the FORCED recovery one and the
    bookkeeping assertions are unambiguous.
    """

    @staticmethod
    def _recording_monitor(mw):
        seen = []
        mw._monitor_degradation = lambda response, session_id: seen.append(response)
        return seen

    def test_t4_forced_compression_recover(self, sid):
        """Scenario T4: first 413 -> forced compression (gates bypassed) ->
        retry succeeds. handler called twice, monitor only on final success."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        err = Provider413Error("413 payload too large")
        calls = []
        requests = []

        def handler(r):
            calls.append(1)
            requests.append(r)
            if len(calls) == 1:
                raise err
            return AIMessage(content="ok")

        monitor = self._recording_monitor(mw)
        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), handler
            )

        assert len(calls) == 2
        assert resp.content == "ok"
        # degradation monitor ran exactly once, on the final successful response
        assert len(monitor) == 1 and monitor[0] is resp
        t4_key = mget("_OVERFLOW_RETRIES_T4_KEY")  # AttributeError-RED probe
        t5_key = mget("_OVERFLOW_RETRIES_T5_KEY")
        assert state_register_mem.get_state(sid, t4_key) == 1
        assert state_register_mem.get_state(sid, t5_key) in (None, 0)
        # exactly one compression this session = the forced recovery one
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        # forced path does NOT arm cooldown / per-turn attempts
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) in (
            None,
            0,
        )
        assert state_register_mem.get_state(sid, mget("_TURN_ATTEMPTS_KEY")) in (
            None,
            0,
        )
        # retry used a REBUILT request (request.override), not the original
        assert requests[1] is not requests[0]
        assert any("trigger=T4" in line and "attempt=1/3" in line for line in lines)
        assert any("error_class=payload_too_large" in line for line in lines)
        assert any("old_tokens=" in line and "new_tokens=" in line for line in lines)

    def test_t5_context_overflow_recover(self, sid):
        """Scenario T5: context-window string error classified as
        context_overflow -> same recovery (trigger=T5)."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        calls = []

        def handler(r):
            calls.append(1)
            if len(calls) == 1:
                raise Exception(T5_OVERFLOW_TEXT)
            return AIMessage(content="ok")

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), handler
            )

        assert len(calls) == 2
        assert resp.content == "ok"
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_T5_KEY")) == 1
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_T4_KEY")) in (
            None,
            0,
        )
        assert any("trigger=T5" in line and "attempt=1/3" in line for line in lines)
        assert any("error_class=context_overflow" in line for line in lines)

    def test_exhausted_retries_propagate_original_error(self, sid):
        """Scenario exhaust: every call 413s -> handler exactly 4 times
        (1 initial + 3 retries), a forced compression before EVERY retry,
        the ORIGINAL exception object re-raised, monitor never called."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        err = Provider413Error("413 payload too large")
        calls = []

        def handler(r):
            calls.append(1)
            raise err

        monitor = self._recording_monitor(mw)
        with capture_logs() as lines:
            with pytest.raises(Provider413Error) as ei:
                mw.wrap_model_call(
                    make_request(t3_low_est_messages(), sid, stub), handler
                )

        assert ei.value is err  # same object: never wrapped, never replaced
        assert len(calls) == 4
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_T4_KEY")) == 3
        # one _apply_compression execution per retry = 3 total
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 3
        assert monitor == []  # failed calls never pollute degradation stats
        assert any("trigger=T4" in line and "attempt=1/3" in line for line in lines)
        assert any("trigger=T4" in line and "attempt=2/3" in line for line in lines)
        assert any("trigger=T4" in line and "attempt=3/3" in line for line in lines)

    def test_non_overflow_error_passthrough_zero_retries(self, sid):
        """Scenario negative: TimeoutError is not a target error -> exactly
        1 handler call, re-raised as-is, zero retry-key writes, no compression."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        err = TimeoutError("timed out")
        calls = []

        def handler(r):
            calls.append(1)
            raise err

        monitor = self._recording_monitor(mw)
        with capture_logs() as lines:
            with pytest.raises(TimeoutError) as ei:
                mw.wrap_model_call(
                    make_request(t3_low_est_messages(), sid, stub), handler
                )

        assert ei.value is err
        assert len(calls) == 1
        assert monitor == []
        for key in (
            mget("_OVERFLOW_RETRIES_T4_KEY"),
            mget("_OVERFLOW_RETRIES_T5_KEY"),
        ):
            assert state_register_mem.get_state(sid, key) in (None, 0)
        assert state_register_mem.get_state(
            sid, mget("_COMPRESSION_COUNT_KEY")
        ) in (None, 0)
        assert not any("trigger=T4" in line or "trigger=T5" in line for line in lines)

    def test_sync_async_recovery_parity(self, sid):
        """Scenario parity: same 413-then-success through wrap and awrap ->
        identical handler call counts, retry keys and final results."""
        sid2 = sid + "-async"
        stub1, stub2 = StubModel(), StubModel()
        mw1 = make_middleware(model=stub1)
        mw2 = make_middleware(model=stub2)
        calls = []

        def handler(r):
            calls.append("s")
            if calls.count("s") == 1:
                raise Provider413Error("413 payload too large")
            return AIMessage(content="ok")

        async def ahandler(r):
            calls.append("a")
            if calls.count("a") == 1:
                raise Provider413Error("413 payload too large")
            return AIMessage(content="ok")

        resp_s = mw1.wrap_model_call(
            make_request(t3_low_est_messages(), sid, stub1), handler
        )
        resp_a = asyncio.run(
            mw2.awrap_model_call(
                make_request(t3_low_est_messages(), sid2, stub2), ahandler
            )
        )

        try:
            t4_key = mget("_OVERFLOW_RETRIES_T4_KEY")
            assert calls.count("s") == calls.count("a") == 2
            assert resp_s.content == resp_a.content == "ok"
            assert state_register_mem.get_state(sid, t4_key) == 1
            assert state_register_mem.get_state(sid2, t4_key) == 1
        finally:
            try:
                state_register_mem.clear_session(sid2)
            except Exception:
                pass

    def test_t4_then_t5_independent_counters(self, sid):
        """Extra: T4 on call 1, T5 on call 2, success on call 3 - the two
        error classes count against INDEPENDENT session keys."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        calls = []

        def handler(r):
            calls.append(1)
            if len(calls) == 1:
                raise Provider413Error("413 payload too large")
            if len(calls) == 2:
                raise Exception(T5_OVERFLOW_TEXT)
            return AIMessage(content="ok")

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), handler
            )

        assert len(calls) == 3
        assert resp.content == "ok"
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_T4_KEY")) == 1
        assert state_register_mem.get_state(sid, mget("_OVERFLOW_RETRIES_T5_KEY")) == 1
        assert any("trigger=T4" in line and "attempt=1/3" in line for line in lines)
        assert any("trigger=T5" in line and "attempt=1/3" in line for line in lines)

    def test_recovery_bypasses_cooldown_gate_and_skip_gate(self, sid):
        """Extra: recovery still works when the wrap call enters via the
        cooldown-gated path (Part A) or the session-skip path (Part B) -
        forced compression bypasses those gates by construction."""
        # Part A: cooldown-gated path
        stub = StubModel()
        mw = make_middleware(model=stub)
        state_register_mem.set_state(sid, mget("_COOLDOWN_ROUNDS_KEY"), 2)
        calls_a = []

        def handler_a(r):
            calls_a.append(1)
            if len(calls_a) == 1:
                raise Provider413Error("413 payload too large")
            return AIMessage(content="ok")

        with capture_logs():
            resp_a = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), handler_a
            )

        t4_key = mget("_OVERFLOW_RETRIES_T4_KEY")
        assert resp_a.content == "ok"
        assert len(calls_a) == 2
        assert state_register_mem.get_state(sid, t4_key) == 1
        # cooldown ticked 2 -> 1 at wrap entry, NOT re-armed by the forced path
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) == 1
        assert state_register_mem.get_state(sid, mget("_TURN_ATTEMPTS_KEY")) in (
            None,
            0,
        )

        # Part B: skip-gated path (session compression total exhausted)
        sid2 = sid + "-skip"
        stub2 = StubModel()
        mw2 = make_middleware(model=stub2)
        state_register_mem.set_state(sid2, mget("_COMPRESSION_COUNT_KEY"), 5)
        calls_b = []

        def handler_b(r):
            calls_b.append(1)
            if len(calls_b) == 1:
                raise Provider413Error("413 payload too large")
            return AIMessage(content="ok")

        try:
            with capture_logs() as lines:
                resp_b = mw2.wrap_model_call(
                    make_request(t3_low_est_messages(), sid2, stub2), handler_b
                )
            assert resp_b.content == "ok"
            assert len(calls_b) == 2
            assert state_register_mem.get_state(sid2, t4_key) == 1
            assert any("attempt=1/3" in line for line in lines)
        finally:
            try:
                state_register_mem.clear_session(sid2)
            except Exception:
                pass


# ======================================================================
# Task 9 Part A: anti-thrash regression matrix
# ======================================================================


def ineffective_noop_messages():
    """est = 82400 // 4 = 20600 in [20480, 20800): hard zone -> T2
    compact_only, but <= budget*2 (20800) so aggressive never fires and
    _determine_cutoff returns 0 (single unsplittable turn) -> strategy
    "noop": zero message-count reduction, zero token reduction < 0.05
    -> counted INEFFECTIVE."""
    return [HumanMessage(content="h" * 82400)]


def compact_then_truncate_messages():
    """est ~27755: hard zone with ONE ToolMessage candidate whose est
    (2002) does NOT cover the overflow (~2158) -> compact_then_truncate
    (auxiliary-LLM summary + target truncation)."""
    return [
        HumanMessage(content="h" * 103000),
        _ai_with_call("p9-c1"),
        ToolMessage(content="x" * 8000, tool_call_id="p9-c1"),
        HumanMessage(content="q2"),
        AIMessage(content="a2"),
        HumanMessage(content="q3"),
        AIMessage(content="a3"),
        HumanMessage(content="q4"),
        AIMessage(content="a4"),
    ]


def _resp_text(resp):
    """Comparable handler outcome: result message contents for
    ModelResponse-shaped responses, content for plain messages."""
    if hasattr(resp, "result"):
        return [str(m.content) for m in resp.result]
    return [str(resp.content)]


class TestAntiThrashMatrix:
    """Task 9 Part A (.omo/plans/context-compression.md item 9a): the
    anti-thrash interactions no earlier test pinned together:

    - ineffective counting x MIN_EFFECTIVENESS_PCT x INEFFECTIVE_THRESHOLD
    - MAX_TOTAL_COMPRESSION_ATTEMPTS x MAX_COMPRESS_ATTEMPTS_PER_TURN
    - forced recovery NOT suppressible by ANY anti-thrash gate
    - TTL registry eviction (record_first_seen) at the real 512 cap
    - middleware budget truncation pairing at 513-pair scale

    Fixture math verified by probe (ctx=41600, usable=25600,
    preserve=10400, budget*2=20800).
    """

    def test_ineffective_compressions_arm_skip_llm_flag(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        count_key = mget("_COMPRESSION_COUNT_KEY")
        ineff_key = mget("_COMPRESSION_INEFFECTIVE_KEY")
        skip_key = mget("_SKIP_LLM_KEY")
        strategy_key = mget("_LAST_STRATEGY_KEY")
        cooldown_key = mget("_COOLDOWN_ROUNDS_KEY")
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        # contract pins
        assert MIN_EFFECTIVENESS_PCT == 0.05
        assert INEFFECTIVE_THRESHOLD == 2

        seen = []

        def handler(r):
            seen.append([str(m.content) for m in r.messages])
            return AIMessage(content="ok")

        # compression #1: 0% reduction -> counted ineffective
        with capture_logs() as lines1:
            mw.wrap_model_call(
                make_request(ineffective_noop_messages(), sid, stub), handler
            )
        assert state_register_mem.get_state(sid, count_key) == 1
        assert state_register_mem.get_state(sid, ineff_key) == 1
        assert state_register_mem.get_state(sid, strategy_key) == "noop"
        assert state_register_mem.get_state(sid, cooldown_key) == 3
        assert state_register_mem.get_state(sid, attempts_key) == 1
        assert state_register_mem.get_state(sid, skip_key) in (None, False)
        assert any("route=compact_only" in line for line in lines1)
        assert seen[-1] == ["h" * 82400]  # zero reduction: identical content

        # compression #2 (cooldown preset 0 so a second consecutive
        # ineffective attempt can be driven)
        state_register_mem.set_state(sid, cooldown_key, 0)
        with capture_logs():
            mw.wrap_model_call(
                make_request(ineffective_noop_messages(), sid, stub), handler
            )
        assert state_register_mem.get_state(sid, count_key) == 2
        assert state_register_mem.get_state(sid, ineff_key) == 2
        assert state_register_mem.get_state(sid, attempts_key) == 2

        # compression #3: streak hits INEFFECTIVE_THRESHOLD -> skip-LLM
        # flag armed; the dispatch STILL runs (the flag only switches the
        # summary source inside _apply_compression)
        state_register_mem.set_state(sid, cooldown_key, 0)
        with capture_logs() as lines3:
            mw.wrap_model_call(
                make_request(ineffective_noop_messages(), sid, stub), handler
            )
        assert state_register_mem.get_state(sid, skip_key) is True
        assert state_register_mem.get_state(sid, count_key) == 3
        assert state_register_mem.get_state(sid, ineff_key) == 3
        assert state_register_mem.get_state(sid, strategy_key) == "noop"
        assert state_register_mem.get_state(sid, cooldown_key) == 3
        assert state_register_mem.get_state(sid, attempts_key) == 3
        assert stub.calls == []  # noop never touches the auxiliary LLM
        assert sum(1 for line in lines3 if "route=compact_only" in line) == 1

    def test_effective_compression_resets_ineffective_streak_and_skip_flag(self, sid):
        """The other half of MIN_EFFECTIVENESS_PCT: one EFFECTIVE compression
        (~98% token reduction via the compact_then_truncate fixture) resets
        both the ineffective streak AND the armed skip-LLM flag ("fallback"
        is in the strategy re-arm list)."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 2)

        seen = []

        def handler(r):
            seen.append([str(m.content) for m in r.messages])
            return AIMessage(content="ok")

        with capture_logs() as lines:
            mw.wrap_model_call(
                make_request(compact_then_truncate_messages(), sid, stub), handler
            )

        assert any("route=compact_then_truncate" in line for line in lines)
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert state_register_mem.get_state(
            sid, mget("_COMPRESSION_INEFFECTIVE_KEY")
        ) == 0
        assert state_register_mem.get_state(sid, mget("_SKIP_LLM_KEY")) is False
        assert state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY")) == "fallback"
        assert state_register_mem.get_state(sid, mget("_COOLDOWN_ROUNDS_KEY")) == 3
        assert state_register_mem.get_state(sid, mget("_TURN_ATTEMPTS_KEY")) == 1
        assert stub.calls == []  # skip-LLM armed -> static fallback summary
        # real reduction reached the model: pair + preserved tail (10 msgs),
        # the 103000-char message is gone
        assert len(seen[0]) == 10
        assert seen[0][0] == "What did we do so far?"
        assert seen[0][1].startswith("[CONTEXT COMPACTION")
        assert all(len(content) < 103000 for content in seen[0])

    def test_session_cap_blocks_proactive_in_new_turns(self, sid):
        """MAX_TOTAL_COMPRESSION_ATTEMPTS=5 suppresses PROACTIVE compression
        on fresh turns (cooldown/attempt gates never even consulted), while
        forced recovery still works (see test_force_bypass_* below)."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        count_key = mget("_COMPRESSION_COUNT_KEY")
        state_register_mem.set_state(sid, count_key, 5)
        # contract pins
        assert MAX_TOTAL_COMPRESSION_ATTEMPTS == 5
        assert MAX_COMPRESS_ATTEMPTS_PER_TURN == 3

        for _ in range(2):
            with capture_logs() as lines:
                mw.wrap_model_call(
                    make_request(hard_overflow_messages(), sid, stub),
                    lambda r: AIMessage(content="ok"),
                )
            assert not any("trigger=" in line for line in lines)
            assert not any("route=" in line for line in lines)
            assert state_register_mem.get_state(sid, count_key) == 5
            assert state_register_mem.get_state(
                sid, mget("_TURN_ATTEMPTS_KEY")
            ) in (None, 0)
            assert state_register_mem.get_state(
                sid, mget("_COOLDOWN_ROUNDS_KEY")
            ) in (None, 0)
        assert stub.calls == []

    def test_session_cap_short_circuits_t3_post_check(self, sid):
        """The session cap also short-circuits the T3 post-response check:
        no dispatch, not even the detailed reported-token log."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        count_key = mget("_COMPRESSION_COUNT_KEY")
        state_register_mem.set_state(sid, count_key, 5)
        mr = ModelResponse(result=[_ai_with_usage("ok", T3_TRIGGER_REPORTED)])

        with capture_logs() as lines:
            resp = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), lambda r: mr
            )

        assert resp is mr
        assert not any("trigger=" in line for line in lines)
        assert not any("reported_input_tokens=" in line for line in lines)
        assert state_register_mem.get_state(sid, count_key) == 5
        assert state_register_mem.get_state(sid, mget("_TURN_ATTEMPTS_KEY")) in (
            None,
            0,
        )

    def test_force_bypass_recovery_overrides_all_anti_thrash_gates(self, sid):
        """THE regression: session cap + cooldown + per-turn attempts all
        exhausted, a 413 STILL recovers via forced compression (Part B),
        while a plain call on the same state is fully suppressed (Part A)."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        count_key = mget("_COMPRESSION_COUNT_KEY")
        cooldown_key = mget("_COOLDOWN_ROUNDS_KEY")
        attempts_key = mget("_TURN_ATTEMPTS_KEY")
        t4_key = mget("_OVERFLOW_RETRIES_T4_KEY")
        state_register_mem.set_state(sid, count_key, 5)
        state_register_mem.set_state(sid, cooldown_key, 2)
        state_register_mem.set_state(sid, attempts_key, 3)

        # Part A control: plain call on exhausted state -> fully suppressed
        calls_a = []

        def handler_a(r):
            calls_a.append(1)
            return AIMessage(content="ok")

        with capture_logs() as lines_a:
            resp_a = mw.wrap_model_call(
                make_request(hard_overflow_messages(), sid, stub), handler_a
            )
        assert len(calls_a) == 1 and resp_a.content == "ok"
        assert not any("route=" in line for line in lines_a)
        assert state_register_mem.get_state(sid, count_key) == 5
        # cooldown ticked 2 -> 1 at wrap entry, NOT re-armed by the skip path
        assert state_register_mem.get_state(sid, cooldown_key) == 1
        assert state_register_mem.get_state(sid, attempts_key) == 3

        # Part B: 413 then success -> forced recovery overrides ALL gates
        monitor = []
        mw._monitor_degradation = lambda response, session_id: monitor.append(response)
        calls_b, requests = [], []

        def handler_b(r):
            calls_b.append(1)
            requests.append(r)
            if len(calls_b) == 1:
                raise Provider413Error("413 payload too large")
            return AIMessage(content="ok")

        with capture_logs() as lines_b:
            resp_b = mw.wrap_model_call(
                make_request(t3_low_est_messages(), sid, stub), handler_b
            )

        assert len(calls_b) == 2
        assert resp_b.content == "ok"
        assert requests[1] is not requests[0]  # rebuilt request, not original
        assert state_register_mem.get_state(sid, t4_key) == 1
        assert state_register_mem.get_state(sid, count_key) == 6  # 5 + forced
        # cooldown ticked again (1 -> 0), never re-armed by the forced path
        assert state_register_mem.get_state(sid, cooldown_key) == 0
        assert state_register_mem.get_state(sid, attempts_key) == 3
        assert state_register_mem.get_state(
            sid, mget("_FORCE_RECOVERY_KEY")
        ) in (None, False)
        assert len(monitor) == 1 and monitor[0] is resp_b
        assert any("trigger=T4" in line and "attempt=1/3" in line for line in lines_b)
        assert any("error_class=payload_too_large" in line for line in lines_b)

    def test_ttl_registry_evicts_oldest_at_real_512_cap(self):
        """record_first_seen at the REAL TTL_REGISTRY_MAX_ENTRIES cap:
        oldest first-seen evicted, registry never exceeds the bound,
        existing entries never overwritten."""
        registry = {}
        for i in range(TTL_REGISTRY_MAX_ENTRIES + 1):
            record_first_seen(
                registry,
                [ToolMessage(content="x" * 10, tool_call_id=f"p9-ttl-{i}")],
                now=float(i),
            )
            assert len(registry) <= TTL_REGISTRY_MAX_ENTRIES
        assert len(registry) == TTL_REGISTRY_MAX_ENTRIES
        assert "p9-ttl-0" not in registry  # smallest first-seen evicted
        assert registry["p9-ttl-512"] == 512.0
        # never-overwrite semantics at the boundary
        record_first_seen(
            registry,
            [ToolMessage(content="y", tool_call_id="p9-ttl-256")],
            now=99999.0,
        )
        assert registry["p9-ttl-256"] == 256.0
        assert len(registry) == TTL_REGISTRY_MAX_ENTRIES

    def test_middleware_truncation_pairing_at_513_pair_scale(self):
        """_run_budget_truncation at 513 tool pairs: budget respected
        (freed >= usable * TRUNCATE_BUDGET_RATIO), in place (length
        unchanged), AI-tool-call/tool-result pairing intact, and no
        ToolMessage ever emptied (non-empty placeholder guarantee)."""
        stub = StubModel()
        mw = make_middleware(model=stub)
        msgs = []
        for i in range(TTL_REGISTRY_MAX_ENTRIES + 1):
            tc = f"p9-par-{i}"
            msgs.append(HumanMessage(content="q"))
            msgs.append(_ai_with_call(tc))
            msgs.append(ToolMessage(content="x" * 8000, tool_call_id=tc))
        usable = 25600  # ctx 41600 - reserve 16000 (make_middleware defaults)
        freed = mw._run_budget_truncation(msgs, usable)
        assert freed >= int(usable * TRUNCATE_BUDGET_RATIO)  # >= 15360 (probe: 15620)
        assert len(msgs) == (TTL_REGISTRY_MAX_ENTRIES + 1) * 3  # none dropped
        ai_ids = []
        for msg in msgs:
            for tc in getattr(msg, "tool_calls", None) or []:
                ai_ids.append(
                    tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                )
        tool_ids = [m.tool_call_id for m in msgs if isinstance(m, ToolMessage)]
        assert ai_ids == tool_ids
        assert all(str(m.content).strip() for m in msgs if isinstance(m, ToolMessage))


# ======================================================================
# Task 9 Part B: sync/async full-branch parity matrix
# ======================================================================


_PARITY_STATE_KEYS = (
    "_COMPRESSION_COUNT_KEY",
    "_TURN_ATTEMPTS_KEY",
    "_COOLDOWN_ROUNDS_KEY",
    "_OVERFLOW_RETRIES_T4_KEY",
    "_OVERFLOW_RETRIES_T5_KEY",
)

_PARITY_413 = Provider413Error("413 payload too large")
_PARITY_T5 = Exception(T5_OVERFLOW_TEXT)

# Every compression branch reachable from wrap_model_call/awrap_model_call:
# fits / truncate / compact-noop / compact-then-truncate / T3 compact /
# T3 truncate / T4 recover / T5 recover / T4 exhaust.
# `aux` = expected auxiliary-LLM call count (probe-verified; None = pin
# parity only).
_PARITY_SCENARIOS = {
    "s1_fits": dict(
        msgs=low_pressure_messages, reported=None, errors=None,
        calls=1, aux=0, state={},
    ),
    "s2_truncate_only": dict(
        msgs=soft_overflow_messages, reported=None, errors=None,
        calls=1, aux=0, state={},
    ),
    "s3_compact_only": dict(
        msgs=hard_overflow_messages, reported=None, errors=None,
        calls=1, aux=0,
        state={
            "_COMPRESSION_COUNT_KEY": 1,
            "_TURN_ATTEMPTS_KEY": 1,
            "_COOLDOWN_ROUNDS_KEY": 3,
        },
    ),
    "s4_compact_then_truncate": dict(
        msgs=compact_then_truncate_messages, reported=None, errors=None,
        calls=1, aux=1,
        state={
            "_COMPRESSION_COUNT_KEY": 1,
            "_TURN_ATTEMPTS_KEY": 1,
            "_COOLDOWN_ROUNDS_KEY": 3,
        },
    ),
    "s5_t3_compact": dict(
        msgs=t3_low_est_messages, reported=T3_TRIGGER_REPORTED, errors=None,
        calls=1, aux=0,
        state={
            "_COMPRESSION_COUNT_KEY": 1,
            "_TURN_ATTEMPTS_KEY": 1,
            "_COOLDOWN_ROUNDS_KEY": 3,
        },
    ),
    "s6_t3_truncate": dict(
        msgs=t3_truncate_messages, reported=T3_TRUNCATE_REPORTED, errors=None,
        calls=1, aux=0, state={},
    ),
    "s7_t4_recover": dict(
        msgs=t3_low_est_messages, reported=None, errors=[_PARITY_413, None],
        calls=2, aux=0,
        state={"_OVERFLOW_RETRIES_T4_KEY": 1, "_COMPRESSION_COUNT_KEY": 1},
    ),
    "s8_t5_recover": dict(
        msgs=t3_low_est_messages, reported=None, errors=[_PARITY_T5, None],
        calls=2, aux=0,
        state={"_OVERFLOW_RETRIES_T5_KEY": 1, "_COMPRESSION_COUNT_KEY": 1},
    ),
    "s9_t4_exhaust": dict(
        msgs=t3_low_est_messages, reported=None, errors=[_PARITY_413],
        calls=4, aux=0,
        state={"_OVERFLOW_RETRIES_T4_KEY": 3, "_COMPRESSION_COUNT_KEY": 3},
    ),
}


def _parity_state_snapshot(sid):
    snap = {}
    for name in _PARITY_STATE_KEYS:
        val = state_register_mem.get_state(sid, mget(name))
        snap[name] = 0 if val is None else val
    return snap


def _trigger_facts(lines):
    """Comparable trigger-log facts per 'trigger=' line: the sorted
    key=value tokens, immune to logger formatting and session-id
    differences between the two twins."""
    facts = []
    for line in lines:
        if "trigger=" not in line:
            continue
        facts.append(
            tuple(
                sorted(
                    p
                    for p in line.split()
                    if "=" in p and not p.startswith("session=")
                )
            )
        )
    return facts


def _run_parity_twin(mw, stub, sid, spec):
    """Run one twin (sync or async) of a parity scenario.

    Returns (outcome, seen, aux, trigger_facts) where outcome is
    ("ok", result-contents) or ("raise", exception type name).
    """
    calls = []
    seen = []
    errors = spec["errors"]
    reported = spec["reported"]

    def handler(r):
        calls.append(1)
        seen.append([str(m.content) for m in r.messages])
        if errors:
            err = errors[min(len(calls), len(errors)) - 1]
            if err is not None:
                raise err
        if reported is not None:
            return ModelResponse(result=[_ai_with_usage("ok", reported)])
        return AIMessage(content="ok")

    async def ahandler(r):
        return handler(r)

    try:
        with capture_logs() as lines:
            if spec["mode"] == "sync":
                resp = mw.wrap_model_call(
                    make_request(spec["msgs"](), sid, stub), handler
                )
            else:
                resp = asyncio.run(
                    mw.awrap_model_call(
                        make_request(spec["msgs"](), sid, stub), ahandler
                    )
                )
        return ("ok", _resp_text(resp)), seen, len(stub.calls), _trigger_facts(lines)
    except Exception as exc:  # noqa: BLE001 - parity probe records raise kind
        return ("raise", type(exc).__name__), seen, len(stub.calls), _trigger_facts(lines)


class TestSyncAsyncFullBranchParity:
    """Task 9 Part B: every compression branch driven through BOTH the sync
    and the async wrap entrypoints with identical fixtures. Twins are
    compared on: handler outcome (result contents or raise kind),
    handler-visible message contents, handler call count, auxiliary-LLM
    call count, trigger-log facts, and the five Task-5/7 state keys
    (normalized None -> 0)."""

    @pytest.mark.parametrize("scenario", sorted(_PARITY_SCENARIOS))
    def test_sync_async_full_branch_parity(self, sid, scenario):
        spec = _PARITY_SCENARIOS[scenario]
        sid_s, sid_a = sid, sid + "-par"
        stub_s, stub_a = StubModel(), StubModel()
        mw_s = make_middleware(model=stub_s)
        mw_a = make_middleware(model=stub_a)
        try:
            outcome_s, seen_s, aux_s, trig_s = _run_parity_twin(
                mw_s, stub_s, sid_s, dict(spec, mode="sync")
            )
            outcome_a, seen_a, aux_a, trig_a = _run_parity_twin(
                mw_a, stub_a, sid_a, dict(spec, mode="async")
            )

            # 1. handler outcome: same result contents or same raise kind
            assert outcome_s == outcome_a
            # 2. handler-visible message contents identical per call
            assert seen_s == seen_a
            # 3. handler call count matches the branch plan
            assert len(seen_s) == len(seen_a) == spec["calls"]
            # 4. auxiliary-LLM usage: pinned when known, else parity-only
            if spec["aux"] is not None:
                assert aux_s == aux_a == spec["aux"]
            else:
                assert aux_s == aux_a
            # 5. trigger-log facts identical
            assert trig_s == trig_a
            # 6. state snapshot: twins equal AND equal to the branch table
            snap_s = _parity_state_snapshot(sid_s)
            snap_a = _parity_state_snapshot(sid_a)
            assert snap_s == snap_a
            expected = {name: 0 for name in _PARITY_STATE_KEYS}
            expected.update(spec["state"])
            assert snap_s == expected
        finally:
            for s in (sid_s, sid_a):
                try:
                    state_register_mem.clear_session(s)
                except Exception:
                    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
