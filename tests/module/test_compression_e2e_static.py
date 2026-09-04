"""Hermetic e2e compression matrix over REAL create_agent assembly (Task 8).

Plan item 8 (.omo/plans/context-compression.md): exercise the Summarization
middleware through the REAL agent graph (create_agent + real tool node +
real middleware hooks) with scripted stub models - zero network, zero LLM.

The SAME six scenarios run against BOTH Summarization registration orders:

- main order   (mirrors agent/core.py):    [IterationBudget(90), Summarization]
  -> Summarization registered LAST (innermost wrap layer).
- worker order (mirrors spawn/core.py):    [Summarization, IterationBudget(60)]
  -> Summarization registered FIRST (outermost wrap layer); worker trigger
  form [("messages", 40), ("tokens", 0.80 * window)], no
  need_update_system_prompt.

Scenario matrix (each parametrized over both orders):

  E2E-A  T2 soft overflow (intra-turn tool growth) -> truncate_tool_results_only,
         no auxiliary-LLM call, no message deletion (pairing intact).
  E2E-B  hard overflow at turn start (non-tool bulk) -> compact_then_truncate
         via the T1 preflight (T2 shares the same _execute_compact path
         mid-turn), auxiliary LLM called exactly once, summary pair in the
         model view, pressure back under the usable budget.
  E2E-C  T3 reported-usage (real usage_metadata on the response) ->
         truncate route mutates state messages IN PLACE, so the next round
         (caller-managed history) sees reduced pressure and zero triggers.
  E2E-D  T4 provider 413 recovery -> forced compression + handler retry,
         per-class retry counter = 1, session continues next round.
  E2E-E  T4 exhaustion (4 consecutive 413) -> retry counter capped at
         MAX_OVERFLOW_RETRIES, ORIGINAL exception object propagates
         (identity preserved through the real chain).
  E2E-F  TTL clock around the real chain -> record_first_seen before the
         turn, select_expired proves the boundary-inclusive expiry of the
         round-1 oversized tool result; two further rounds stay stable
         (no deletion, pairing intact, length only grows).

Window math (CTX_WINDOW = 41600):
  usable_budget        = 41600 - COMPRESSION_RESERVE_TOKENS(16000) = 25600
  threshold_truncate   = usable * PREEMPTIVE_TRUNCATE_RATIO(0.70) = 17920
  threshold_compact    = usable * COMPRESSION_TRIGGER_RATIO(0.80) = 20480
  truncate budget      = usable * TRUNCATE_BUDGET_RATIO(0.6)   = 15360

Known deviation from production wiring (documented): the auxiliary
summarizer is a SEPARATE stub object from the main-model stub. In
spawn/core.py the worker reuses the child LLM object as auxiliary_llm
(aliasing); aliasing it here would make the aux call counter
indistinguishable from main-model calls, so the stubs are separate
observably. The middleware receives a distinct object either way.

Hermeticity: no network, no llm_e2e marker (auto-collected by Group B of
tests/run_tests_split.py), unique per-test session ids, state-register
cleanup in teardown.
"""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from loguru import logger

from agent.middlewares import IterationBudget, Summarization
from agent.middlewares.summarization import (
    _COOLDOWN_ROUNDS_KEY,
    _COMPRESSION_COUNT_KEY,
    _OVERFLOW_RETRIES_T4_KEY,
)
from config.num import (
    COMPACTION_COOLDOWN_ROUNDS,
    COMPRESSION_RESERVE_TOKENS,
    COMPRESSION_TRIGGER_RATIO,
    PREEMPTIVE_TRUNCATE_RATIO,
    PRUNE_TTL_SECONDS,
)
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens
from pub_func.message.tool_result_ttl import (
    TTL_PLACEHOLDER,
    record_first_seen,
    select_expired,
)
from runtime.state_register import state_register_db, state_register_mem

# ----------------------------------------------------------------------
# Window math (mirrors the numbers used by the Task 3-7 suites)
# ----------------------------------------------------------------------

CTX_WINDOW = 41600
USABLE_BUDGET = CTX_WINDOW - COMPRESSION_RESERVE_TOKENS  # 25600
THR_TRUNC = int(USABLE_BUDGET * PREEMPTIVE_TRUNCATE_RATIO)  # 17920
THR_CMP = int(USABLE_BUDGET * COMPRESSION_TRIGGER_RATIO)  # 20480


# ----------------------------------------------------------------------
# Stub models (scripted, no network)
# ----------------------------------------------------------------------


class _ScriptedMainModel:
    """create_agent main-model stub.

    ``script`` entries are consumed FIFO per ainvoke call:
      - AIMessage instance -> returned as the model response;
      - BaseException instance -> RAISED (provider-error simulation).
    ``calls`` records a DEEP COPY of every received message list, so later
    in-place truncation of state message objects cannot rewrite history.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list] = []

    def bind(self, **kwargs):  # create_agent binds tools/kwargs - ignore
        return self

    def bind_tools(self, tools, **kwargs):  # real factory path when tools exist
        return self

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(copy.deepcopy(list(messages)))
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


class _StubSummaryModel:
    """Auxiliary summarizer stub (``.text`` protocol of _create_summary)."""

    _TEXT = (
        "Compressed summary: earlier history compacted; prior tool outputs "
        "were folded into this note and the session goal remains unchanged."
    )

    def __init__(self):
        self.calls: list[str] = []

    def _respond(self, prompt):
        self.calls.append(str(prompt))
        return SimpleNamespace(text=self._TEXT)

    def invoke(self, prompt, config=None, **kwargs):
        return self._respond(prompt)

    async def ainvoke(self, prompt, config=None, **kwargs):
        return self._respond(prompt)


class Provider413Error(Exception):
    """413-shaped provider error (classifier channel 1: status_code attr)."""

    status_code = 413


# ----------------------------------------------------------------------
# Real tool + chain builders (dual registration orders)
# ----------------------------------------------------------------------


@tool
def probe(size: int, fill: str) -> str:
    """Return a deterministic filler string for compression testing."""
    return fill * int(size)


class _E2EState(AgentState):
    session_id: str


def _build_agent(order: str, main_model, aux_model):
    """Assemble the real graph with the plan-mirroring middleware orders."""
    trigger_tokens = int(CTX_WINDOW * COMPRESSION_TRIGGER_RATIO)
    if order == "main":
        middleware = [
            IterationBudget(90),
            Summarization(
                need_update_system_prompt=True,
                model=aux_model,
                main_llm_context_window=CTX_WINDOW,
                trigger=[("tokens", trigger_tokens)],
                keep=("messages", 10),
            ),
        ]
    elif order == "worker":
        middleware = [
            Summarization(
                model=aux_model,
                main_llm_context_window=CTX_WINDOW,
                trigger=[("messages", 40), ("tokens", trigger_tokens)],
                keep=("messages", 10),
            ),
            IterationBudget(60),
        ]
    else:  # pragma: no cover - fixture constrains the values
        raise ValueError(f"unknown order: {order}")
    return create_agent(
        model=main_model,
        tools=[probe],
        middleware=middleware,
        state_schema=_E2EState,
    )


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(params=["main", "worker"])
def order(request):
    """Run every scenario under BOTH registration orders."""
    return request.param


@pytest.fixture
def sid(request):
    """Unique per-test session id with state-register cleanup."""
    token = "t8e2e-" + request.node.name[:48] + "-" + uuid.uuid4().hex[:6]
    yield token
    try:
        state_register_mem.clear_session(token)
    except Exception:  # pragma: no cover - teardown best effort
        pass
    try:
        state_register_db.delete_state(token, "system_prompt")
    except Exception:  # pragma: no cover - teardown best effort
        pass


class _LogCapture:
    """Capture loguru records at INFO+ (route=T* info, T4 attempts warning)."""

    def __init__(self):
        self.lines: list[str] = []
        self._handler_id = None

    def _sink(self, message):
        self.lines.append(str(message))

    def __enter__(self):
        self._handler_id = logger.add(self._sink, level="INFO")
        return self

    def __exit__(self, *exc_info):
        logger.remove(self._handler_id)
        return False

    def route_lines(self) -> list[str]:
        return [
            line
            for line in self.lines
            if "Context compression" in line and "route=" in line
        ]

    def compression_lines(self) -> list[str]:
        return [line for line in self.lines if "Context compression" in line]


# ----------------------------------------------------------------------
# Assertion helpers
# ----------------------------------------------------------------------


def _est(messages) -> int:
    return sum(estimate_msg_tokens(m) for m in messages)


def _assert_pairing(messages) -> None:
    """Tool-call/tool-result pairing invariant: no orphan, no empty result."""
    ai_ids: set[str] = set()
    tool_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                ai_ids.add(tc["id"])
        elif isinstance(msg, ToolMessage):
            tool_ids.add(msg.tool_call_id)
            assert msg.content, (
                f"empty ToolMessage content for {msg.tool_call_id!r}"
            )
    assert ai_ids == tool_ids, (
        f"pairing broken: ai_only={ai_ids - tool_ids} tool_only={tool_ids - ai_ids}"
    )


def _find_tool_message(messages, tc_id: str) -> ToolMessage:
    matches = [
        m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == tc_id
    ]
    assert len(matches) == 1, f"expected exactly one ToolMessage for {tc_id!r}"
    return matches[0]


def _log_int(line: str, field: str) -> int:
    match = re.search(rf"{field}=(\d+)", line)
    assert match, f"{field} not found in log line: {line!r}"
    return int(match.group(1))


def _strip_system(view: list) -> list:
    """Conversation-only view for order-invariant comparisons.

    Main order (need_update_system_prompt=True) legitimately PREPENDS a
    rebuilt system prompt to the model view after an actual compression;
    strip those to compare conversation content without weakening the
    never-invent/never-drop/never-mutate invariant.
    """
    return [m for m in view if not isinstance(m, SystemMessage)]


def _tool_call_entry(tc_id: str, size: int, fill: str) -> dict:
    return {"name": "probe", "args": {"size": size, "fill": fill}, "id": tc_id}


def _hist9(tool_content: str, tc_id: str) -> list:
    """9-message caller history whose only tool result holds ``tool_content``.

    Shape: [H, AI(tc), Tool, H, AI, H, AI, H, AI] - small except the single
    tool result, so the overflow math is driven by that one message.
    """
    fill = tool_content[0] if tool_content else "s"
    return [
        HumanMessage("q1"),
        AIMessage("", tool_calls=[_tool_call_entry(tc_id, len(tool_content), fill)]),
        ToolMessage(tool_content, tool_call_id=tc_id),
        HumanMessage("q2"),
        AIMessage("a2"),
        HumanMessage("q3"),
        AIMessage("a3"),
        HumanMessage("q4"),
        AIMessage("a4"),
    ]


# ======================================================================
# E2E-A - T2 soft overflow via real tool loop -> truncate track
# ======================================================================


def test_e2e_a_t2_soft_overflow_truncate_track(order, sid):
    """Intra-turn growth pushes pressure into the soft band [17920, 20480).

    The big tool result only becomes a truncatable candidate once it ages
    past the TRUNCATABLE_RECENT_SKIP(6) tail window (>= 7 later messages),
    so the truncate route must fire at the FINAL model call, cut the big
    tool result IN PLACE, and never call the auxiliary LLM.
    """
    big_tc = "e2eA-big-1"
    script = [
        AIMessage("", tool_calls=[_tool_call_entry(big_tc, 76000, "x")]),
        AIMessage(
            "",
            tool_calls=[
                _tool_call_entry("e2eA-s1", 40, "s"),
                _tool_call_entry("e2eA-s2", 40, "s"),
                _tool_call_entry("e2eA-s3", 40, "s"),
                _tool_call_entry("e2eA-s4", 40, "s"),
                _tool_call_entry("e2eA-s5", 40, "s"),
            ],
        ),
        AIMessage("Final answer for E2E-A."),
    ]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    with _LogCapture() as cap:
        result = asyncio.run(
            agent.ainvoke({"messages": _hist9("s" * 200, "hist-tc1"), "session_id": sid})
        )

    # --- route log: exactly one T2 truncate route, no compact anywhere ---
    routes = cap.route_lines()
    assert len(routes) == 1, f"expected exactly one route log, got {routes}"
    assert "trigger=T2" in routes[0]
    assert "route=truncate_tool_results_only" in routes[0]
    assert _log_int(routes[0], "old_tokens") >= THR_TRUNC
    assert _log_int(routes[0], "new_tokens") < THR_CMP
    assert _log_int(routes[0], "new_tokens") < _log_int(routes[0], "old_tokens")
    assert not any("route=compact" in line for line in cap.compression_lines())

    # --- no auxiliary-LLM call: truncation track is LLM-free ---
    assert aux_model.calls == []

    # --- three model calls; the big result truncates only at the last one ---
    assert len(main_model.calls) == 3
    pre_trunc = _find_tool_message(main_model.calls[1], big_tc)
    assert pre_trunc.content == "x" * 76000, "big result must be intact at call 2"
    post_trunc = _find_tool_message(main_model.calls[2], big_tc)
    assert len(post_trunc.content) < 76000
    assert "truncated by context compression" in post_trunc.content
    # final call view: full 17-message transcript, NOTHING deleted
    assert len(main_model.calls[2]) == 17

    # --- graph state: in-place mutation observable, pairing intact ---
    assert len(result["messages"]) == 18
    state_big = _find_tool_message(result["messages"], big_tc)
    assert len(state_big.content) < 76000
    _assert_pairing(result["messages"])
    assert result["messages"][-1].content == "Final answer for E2E-A."

    # --- no LLM compression bookkeeping for the truncate-only track ---
    assert state_register_mem.get_state(sid, _COMPRESSION_COUNT_KEY, 0) == 0


# ======================================================================
# E2E-B - hard overflow -> compact_then_truncate (LLM summary mandatory)
# ======================================================================


def test_e2e_b_hard_overflow_compact_llm_summary(order, sid):
    """Hard band (>= 20480) with the bulk in NON-tool text (a 104000-char
    HumanMessage leading the history).

    The non-LLM strategies (dedup / prune / target truncation) only rewrite
    ToolMessage content, so they cannot cover the overflow: the compact
    route runs and the auxiliary-LLM summary becomes mandatory. The route
    fires at the T1 preflight (turn start); T2 dispatches the exact same
    _execute_compact path mid-turn, and a tool-result-bulk hard overflow is
    always resolved by target truncation before the LLM step (verified in
    source and in the first draft of this suite).
    """
    huge_tc = "hist-tc1"
    round1_input = [HumanMessage("y" * 104000), *_hist9("x" * 1000, huge_tc)]
    script = [
        AIMessage("Final answer for E2E-B."),
        AIMessage("Round-2 reply for E2E-B."),
    ]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    # --- round 1: T1 preflight compacts BEFORE any model call ---
    with _LogCapture() as cap:
        result1 = asyncio.run(
            agent.ainvoke({"messages": round1_input, "session_id": sid})
        )

    # --- route log: exactly one compact_then_truncate ---
    routes = cap.route_lines()
    assert len(routes) == 1, f"expected exactly one route log, got {routes}"
    assert "trigger=T1" in routes[0]
    assert "route=compact_then_truncate" in routes[0]
    old_tokens = _log_int(routes[0], "old_tokens")
    new_tokens = _log_int(routes[0], "new_tokens")
    assert old_tokens >= THR_CMP
    assert new_tokens < old_tokens
    assert new_tokens < USABLE_BUDGET

    # --- exactly one auxiliary-LLM summary call ---
    assert len(aux_model.calls) == 1

    # --- compacted model view: summary pair present, under usable budget ---
    assert len(main_model.calls) == 1
    compacted = main_model.calls[0]
    summary_ais = [
        m
        for m in compacted
        if getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization"
    ]
    # _build_new_messages stamps lc_source on the AI part only; the paired
    # Human question sits directly in front of it.
    assert len(summary_ais) == 1, "summary AIMessage (lc_source) missing"
    pair_idx = compacted.index(summary_ais[0])
    assert pair_idx >= 1 and isinstance(compacted[pair_idx - 1], HumanMessage)
    assert compacted[pair_idx - 1].content == "What did we do so far?"
    assert _est(compacted) < USABLE_BUDGET
    assert all(
        m.content != "y" * 104000 for m in compacted
    ), "huge human message must be summarized away"
    _assert_pairing(compacted)

    # --- T1 rewrote the graph state too: summary + preserved tail + reply ---
    assert len(result1["messages"]) == 12
    assert result1["messages"][-1].content == "Final answer for E2E-B."
    assert all(
        m.content != "y" * 104000 for m in result1["messages"]
    ), "huge human message must not survive in state"
    _assert_pairing(result1["messages"])

    # --- anti-thrash bookkeeping: one compression, cooldown armed ---
    assert state_register_mem.get_state(sid, _COMPRESSION_COUNT_KEY, 0) == 1
    # The compact armed the cooldown at COMPACTION_COOLDOWN_ROUNDS during the
    # T1 preflight; the round-1 wrap_model_call then ticked it down by one.
    assert (
        state_register_mem.get_state(sid, _COOLDOWN_ROUNDS_KEY, 0)
        == COMPACTION_COOLDOWN_ROUNDS - 1
    )

    # --- round 2: caller passes the compacted state back; no new triggers ---
    with _LogCapture() as cap2:
        result2 = asyncio.run(
            agent.ainvoke(
                {
                    "messages": [*result1["messages"], HumanMessage("follow-up")],
                    "session_id": sid,
                }
            )
        )
    assert cap2.route_lines() == []
    assert len(result2["messages"]) == 14
    assert result2["messages"][-1].content == "Round-2 reply for E2E-B."
    _assert_pairing(result2["messages"])


# ======================================================================
# E2E-C - T3 reported-usage -> truncate + next-round pressure reduction
# ======================================================================


def test_e2e_c_t3_reported_usage_truncate_reduces_next_round(order, sid):
    """Input fits the estimator (16501 < 17920) but the response carries
    real usage_metadata reporting input_tokens=26000 (>= 20480) -> the T3
    post-response check routes to truncation, mutating the state message
    IN PLACE. Round 2 (caller passes round-1 result back) then sees the
    reduced pressure: zero compression triggers, truncated tool result,
    pairing intact, no auxiliary-LLM call anywhere.
    """
    big_tc = "hist-tc1"
    reported = AIMessage(
        "Final answer for E2E-C.",
        usage_metadata={
            "input_tokens": 26000,
            "output_tokens": 10,
            "total_tokens": 26010,
        },
    )
    script = [reported, AIMessage("Round-2 reply for E2E-C.")]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    # --- round 1: estimator fits, T3 catches the reported usage ---
    with _LogCapture() as cap:
        result1 = asyncio.run(
            agent.ainvoke({"messages": _hist9("x" * 66000, big_tc), "session_id": sid})
        )

    # T3 logs twice at INFO: the _log_route line plus the post-response
    # line carrying reported_input_tokens - count only the former.
    routes = [l for l in cap.route_lines() if "reported_input_tokens" not in l]
    assert len(routes) == 1, (
        f"expected exactly one route log, got {cap.route_lines()}"
    )
    assert "trigger=T3" in routes[0]
    assert "reported_input_tokens=26000" in " ".join(cap.compression_lines())
    assert "route=truncate_tool_results_only" in routes[0]
    assert _log_int(routes[0], "new_tokens") < _log_int(routes[0], "old_tokens")
    assert aux_model.calls == []
    assert len(main_model.calls) == 1

    # --- in-place state mutation visible in round-1 result ---
    assert len(result1["messages"]) == 10
    big1 = _find_tool_message(result1["messages"], big_tc)
    assert len(big1.content) < 66000
    assert "truncated by context compression" in big1.content
    _assert_pairing(result1["messages"])

    # --- round 2: reduced pressure -> no triggers, stable pairing ---
    with _LogCapture() as cap2:
        result2 = asyncio.run(
            agent.ainvoke(
                {
                    "messages": [*result1["messages"], HumanMessage("follow-up one")],
                    "session_id": sid,
                }
            )
        )
    assert cap2.route_lines() == [], (
        f"round 2 must not trigger compression: {cap2.route_lines()}"
    )
    assert len(result2["messages"]) == 12
    big2 = _find_tool_message(result2["messages"], big_tc)
    assert big2.content == big1.content, "round 2 must not re-truncate"
    _assert_pairing(result2["messages"])
    assert result2["messages"][-1].content == "Round-2 reply for E2E-C."
    assert len(aux_model.calls) == 0


# ======================================================================
# E2E-D - T4 provider-413 recovery (session continues)
# ======================================================================


def test_e2e_d_t4_provider_413_recovery(order, sid):
    """First model call raises a 413-shaped error -> T4 forced compression
    (noop cutoff for a single-message history, still counted) + handler
    retry succeeds. The per-class retry counter lands on 1, the attempt log
    carries attempt=1/3 + error_class=payload_too_large, and the session
    continues normally on the next round.
    """
    err = Provider413Error("HTTP 413: request payload too large")
    script = [
        err,
        AIMessage("Recovered answer for E2E-D."),
        AIMessage("Round-2 reply for E2E-D."),
    ]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    # --- round 1: 413 -> forced compression -> retry succeeds ---
    with _LogCapture() as cap:
        result1 = asyncio.run(
            agent.ainvoke({"messages": [HumanMessage("h" * 47000)], "session_id": sid})
        )

    assert len(main_model.calls) == 2, "T4 must retry the handler exactly once"
    assert result1["messages"][-1].content == "Recovered answer for E2E-D."

    attempt_lines = [
        line
        for line in cap.compression_lines()
        if "attempt=1/3" in line and "error_class=payload_too_large" in line
    ]
    assert len(attempt_lines) == 1, (
        f"expected one T4 attempt log, got {cap.compression_lines()}"
    )
    assert "trigger=T4" in attempt_lines[0]

    # --- per-class retry counter armed; forced compression was recorded ---
    assert state_register_mem.get_state(sid, _OVERFLOW_RETRIES_T4_KEY, 0) == 1
    assert state_register_mem.get_state(sid, _COMPRESSION_COUNT_KEY, 0) == 1

    # --- single-message history: compact is a noop (no LLM, no routes) ---
    assert aux_model.calls == []
    assert cap.route_lines() == []
    assert all(
        any(isinstance(m, HumanMessage) and m.content == "h" * 47000 for m in view)
        for view in main_model.calls
    ), "noop forced compression must not drop the user message from any view"

    # --- round 2: session continues normally, no new recovery attempts ---
    with _LogCapture() as cap2:
        result2 = asyncio.run(
            agent.ainvoke(
                {
                    "messages": [*result1["messages"], HumanMessage("follow-up")],
                    "session_id": sid,
                }
            )
        )
    assert result2["messages"][-1].content == "Round-2 reply for E2E-D."
    assert len(main_model.calls) == 3
    assert not any("attempt=1/3" in line for line in cap2.compression_lines())


# ======================================================================
# E2E-E - T4 exhaustion -> ORIGINAL exception identity propagates
# ======================================================================


def test_e2e_e_t4_exhaustion_propagates_original_error(order, sid):
    """Four consecutive 413s: initial call + MAX_OVERFLOW_RETRIES(3) forced
    recoveries, then the retry loop gives up and the ORIGINAL exception
    OBJECT re-raises out of ainvoke (identity preserved through the real
    graph) - never swallowed, never replaced by an empty response.
    """
    err = Provider413Error("HTTP 413: persistent payload too large")
    script = [err, err, err, err]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    with _LogCapture() as cap, pytest.raises(Provider413Error) as excinfo:
        asyncio.run(
            agent.ainvoke({"messages": [HumanMessage("h" * 47000)], "session_id": sid})
        )

    # --- the original exception OBJECT propagated (not a copy/wrap) ---
    assert excinfo.value is err

    # --- exactly MAX_OVERFLOW_RETRIES forced recoveries happened ---
    assert len(main_model.calls) == 4, "1 initial + 3 retried handler calls"
    assert state_register_mem.get_state(sid, _OVERFLOW_RETRIES_T4_KEY, 0) == 3
    assert state_register_mem.get_state(sid, _COMPRESSION_COUNT_KEY, 0) == 3

    # --- attempt logs 1/3, 2/3, 3/3 and the exhaustion error log ---
    for attempt in (1, 2, 3):
        marker = f"attempt={attempt}/3"
        assert any(
            marker in line and "error_class=payload_too_large" in line
            for line in cap.compression_lines()
        ), f"missing T4 attempt log {marker}: {cap.compression_lines()}"
    exhausted = [
        line for line in cap.compression_lines() if "retries exhausted" in line
    ]
    assert len(exhausted) == 1 and "trigger=T4" in exhausted[0]

    # retry views keep the user message: noop compact never drops it
    assert all(
        any(isinstance(m, HumanMessage) and m.content == "h" * 47000 for m in view)
        for view in main_model.calls
    )
    assert aux_model.calls == []


# ======================================================================
# E2E-F - TTL clock around the real chain + multi-round stability
# ======================================================================


def test_e2e_f_ttl_clock_and_multi_round_stability(order, sid):
    """Round-1 input overflows the soft band -> T1 truncate track cuts the
    oversized tool result IN PLACE before the turn starts. The TTL registry
    (record_first_seen before the turn / select_expired after) proves the
    boundary-inclusive expiry of that same tool result at exactly
    PRUNE_TTL_SECONDS - the truncate track is the TTL=0-equivalent executor
    of the same candidate rule. Two further caller-managed rounds stay
    stable: no triggers, no deletions, length only grows, pairing intact.
    """
    big_tc = "hist-tc1"
    round1_input = _hist9("x" * 76000, big_tc)
    script = [
        AIMessage("Round-1 reply."),
        AIMessage("Round-2 reply."),
        AIMessage("Round-3 reply."),
    ]
    main_model = _ScriptedMainModel(script)
    aux_model = _StubSummaryModel()
    agent = _build_agent(order, main_model, aux_model)

    # --- TTL registry: record first-seen BEFORE the turn (wall clock) ---
    registry: dict[str, float] = {}
    record_first_seen(registry, round1_input, now=1000.0)
    assert registry == {big_tc: 1000.0}

    with _LogCapture() as cap:
        result1 = asyncio.run(
            agent.ainvoke({"messages": round1_input, "session_id": sid})
        )

    # --- T1 truncate route fired once, in place, LLM-free ---
    routes = cap.route_lines()
    assert len(routes) == 1, f"expected exactly one route log, got {routes}"
    assert "trigger=T1" in routes[0]
    assert "route=truncate_tool_results_only" in routes[0]
    assert _log_int(routes[0], "new_tokens") < _log_int(routes[0], "old_tokens")
    assert aux_model.calls == []

    assert len(result1["messages"]) == 10
    big1 = _find_tool_message(result1["messages"], big_tc)
    assert len(big1.content) < 76000
    assert TTL_PLACEHOLDER in big1.content
    _assert_pairing(result1["messages"])
    _assert_pairing(main_model.calls[0])

    # --- TTL clock: boundary-inclusive expiry at exactly PRUNE_TTL_SECONDS ---
    before_boundary = select_expired(
        registry, result1["messages"], ttl_seconds=PRUNE_TTL_SECONDS, now=1299.0
    )
    assert before_boundary == [], "299s < 300s must NOT count as expired"
    at_boundary = select_expired(
        registry, result1["messages"], ttl_seconds=PRUNE_TTL_SECONDS, now=1300.0
    )
    expected_idx = next(
        i
        for i, m in enumerate(result1["messages"])
        if isinstance(m, ToolMessage) and m.tool_call_id == big_tc
    )
    assert at_boundary == [expected_idx], (
        f"300s >= 300s must count as expired at index {expected_idx}"
    )

    # --- rounds 2-3: no triggers, stable truncation, length only grows ---
    with _LogCapture() as cap2:
        result2 = asyncio.run(
            agent.ainvoke(
                {
                    "messages": [*result1["messages"], HumanMessage("follow-up one")],
                    "session_id": sid,
                }
            )
        )
    assert cap2.route_lines() == []
    assert len(result2["messages"]) == 12

    with _LogCapture() as cap3:
        result3 = asyncio.run(
            agent.ainvoke(
                {
                    "messages": [*result2["messages"], HumanMessage("follow-up two")],
                    "session_id": sid,
                }
            )
        )
    assert cap3.route_lines() == []
    assert len(result3["messages"]) == 14

    big3 = _find_tool_message(result3["messages"], big_tc)
    assert big3.content == big1.content, "no re-truncation in later rounds"
    for messages in (result2["messages"], result3["messages"]):
        _assert_pairing(messages)
    assert result3["messages"][-1].content == "Round-3 reply."
    assert len(main_model.calls) == 3
    assert len(aux_model.calls) == 0
