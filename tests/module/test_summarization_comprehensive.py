"""Middleware-level comprehensive suite for the Summarization redesign (PART2 §13 adapted, TDD RED baseline)."""

import asyncio
import uuid

import pytest
from types import SimpleNamespace

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)
from langchain.agents.middleware import ModelRequest

import agent.middlewares.summarization as summarization_module
from pub_func.message import estimate_msg_tokens, estimate_messages_tokens
from runtime import state_register_mem
from config.num import (
    COMPLETED_MAX_ITEMS,
    FILE_OPS_LIST_MAX_CHARS,
    KEY_DECISIONS_MAX_ITEMS,
    LATEST_USER_REQUEST_MAX_CHARS,
    CRITICAL_CONTEXT_MAX_ITEMS,
    SUMMARY_TOTAL_MAX_CHARS,
)

# CRITICAL INVARIANT: this module never does
# ``from agent.middlewares.summarization import <name>`` for NEW names —
# the OLD middleware lacks the new module-level names and a top-level
# import would break collection. Access new module-level names at runtime
# via mget(name) == getattr(summarization_module, name); access new
# class/instance methods via plain attribute access (also runtime-resolved).


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


def make_request(messages, session_id="t8-x", model=None):
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
        main_llm_context_window=overrides.pop("main_llm_context_window", 40000),
        need_update_system_prompt=overrides.pop("need_update_system_prompt", False),
    )
    kwargs.update(overrides)
    return summarization_module.Summarization(**kwargs)


def mget(name):
    return getattr(summarization_module, name)


@pytest.fixture
def sid(request):
    s = "t8-" + request.node.name[:40] + "-" + uuid.uuid4().hex[:6]
    yield s
    try:
        state_register_mem.clear_session(s)
    except Exception:
        pass


# ======================================================================
# Token estimation
# ======================================================================


class TestEstimateMsgTokens:
    """Per-message token estimation must delegate to pub_func helpers."""

    def test_class_level_estimate_msg_tokens_delegates(self):
        # Staticmethod on the NEW class: 11 chars // 4 == 2.
        # Red on the old middleware: AttributeError (name does not exist).
        assert summarization_module.Summarization._estimate_msg_tokens(
            AIMessage(content="hello world")
        ) == 2

    def test_instance_estimate_msg_tokens(self):
        mw = make_middleware()
        assert mw._estimate_msg_tokens(HumanMessage(content="abcd")) == 1

    def test_estimate_matches_module_helper(self):
        mw = make_middleware()
        msg = AIMessage(content="x" * 13)
        assert mw._estimate_msg_tokens(msg) == estimate_msg_tokens(msg)

    def test_estimate_tokens_sums_per_message(self):
        # NEW semantics: per-message floor — 2//4 == 0 and 2//4 == 0 → sum 0.
        # Old middleware divides TOTAL chars by 4 once → 4//4 == 1 (Red).
        mw = make_middleware()
        messages = [HumanMessage(content="ab"), AIMessage(content="cd")]
        assert mw._estimate_tokens(messages) == 0

    def test_estimate_tokens_includes_tool_metadata(self):
        mw = make_middleware()
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "a.py"}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="x", tool_call_id="call_1"),
        ]
        assert mw._estimate_tokens(messages) > 0
        assert mw._estimate_tokens(messages) == estimate_messages_tokens(messages)


# ======================================================================
# Config: preserve budget / trigger / FIFO constants
# ======================================================================


class TestSummarizationConfig:
    """Preserve budget clamp, trigger boundary, and FIFO constant lock."""

    def test_preserve_budget_mid_and_none(self):
        # DOC WINS (PART2 §9.7): _calculate_preserve_budget() takes no
        # argument — it reads self._main_llm_context_window, unlike the
        # plan's _calculate_preserve_budget(40000) form. 25% of 40000 = 10000.
        mw = make_middleware(main_llm_context_window=40000)
        assert mw._calculate_preserve_budget() == 10000
        mw_none = make_middleware(main_llm_context_window=None)
        assert mw_none._calculate_preserve_budget() == 2000

    def test_preserve_budget_floor_and_cap(self):
        # 2000 * 0.25 = 500 → floored to MIN_PRESERVE_TOKENS (2000);
        # 200000 * 0.25 = 50000 → capped at MAX_PRESERVE_TOKENS (15000).
        mw_floor = make_middleware(main_llm_context_window=2000)
        assert mw_floor._calculate_preserve_budget() == 2000
        mw_cap = make_middleware(main_llm_context_window=200000)
        assert mw_cap._calculate_preserve_budget() == 15000

    def test_check_trigger_messages_boundary(self):
        mw = make_middleware(trigger=[("messages", 5)])
        five = [HumanMessage(content="hi") for _ in range(5)]
        four = [HumanMessage(content="hi") for _ in range(4)]
        assert mw._check_trigger(five) is True
        assert mw._check_trigger(four) is False

    def test_check_trigger_tokens_effective_max(self):
        # Tokens trigger uses max(local estimate, reported usage tokens).
        mw = make_middleware(trigger=[("tokens", 1000)])
        reported_big = AIMessage(
            content="ab",
            usage_metadata={
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 5000,
            },
        )
        assert mw._check_trigger([reported_big]) is True
        tiny = AIMessage(content="ab", usage_metadata=None)
        assert mw._check_trigger([tiny]) is False

    def test_fifo_constants_lock(self):
        assert COMPLETED_MAX_ITEMS == 5
        assert KEY_DECISIONS_MAX_ITEMS == 5
        assert CRITICAL_CONTEXT_MAX_ITEMS == 3


# ======================================================================
# need_update_system_prompt: hard-requirement dual write + override
# ======================================================================


class TestNeedUpdateSystemPrompt:
    """After compression with need_update_system_prompt=True the rebuilt
    prompt is dual-written (mem + db) and injected as system_message.

    All 3 cases are Red on the old middleware: its _apply_compression has
    signature (state, request, res, session_id) → calling it as
    (request, session_id) raises TypeError. That is EXPECTED and correct.
    """

    def _patch_pipeline(self, monkeypatch, recorder, sid_value, raise_exc=False):
        def fake_build_system_prompt(session_id=""):
            if raise_exc:
                raise RuntimeError("boom")
            return f"T8PROMPT::{session_id}"

        monkeypatch.setattr(
            summarization_module, "build_system_prompt", fake_build_system_prompt
        )

        # The middleware does a call-time `from agent.tools import
        # memory_store; memory_store.load_from_disk()`. memory_store is the
        # re-exported MemoryStore INSTANCE, so shadowing the bound method on
        # the instance intercepts the reload.
        from agent.tools import memory_store

        monkeypatch.setattr(memory_store, "load_from_disk", lambda: None)

        # §9.7 call shape: state_register_db.set_state(session_id, key, value)
        monkeypatch.setattr(
            summarization_module,
            "state_register_db",
            SimpleNamespace(
                set_state=lambda s, k, v: recorder.append((s, k, v))
            ),
        )

    def test_true_dual_write_and_override(self, monkeypatch):
        sid_value = "t8-hreq-true"
        recorder = []
        self._patch_pipeline(monkeypatch, recorder, sid_value)
        try:
            state_register_mem.clear_session(sid_value)
            mw = make_middleware(need_update_system_prompt=True)
            req = make_request(
                [HumanMessage(content="hi"), AIMessage(content="hello")],
                session_id=sid_value,
            )
            result = mw._apply_compression(req, sid_value)

            # Dual write: db recorder captured the write (mem write went to
            # the real state_register_mem).
            assert (sid_value, "system_prompt", f"T8PROMPT::{sid_value}") in recorder
            assert (
                state_register_mem.get_state(sid_value, "system_prompt")
                == f"T8PROMPT::{sid_value}"
            )
            # The returned request carries the rebuilt prompt override.
            assert result.system_message is not None
            assert result.system_message.content == f"T8PROMPT::{sid_value}"
        finally:
            try:
                state_register_mem.clear_session(sid_value)
            except Exception:
                pass

    def test_false_no_write_no_override(self, monkeypatch):
        sid_value = "t8-hreq-false"
        recorder = []
        self._patch_pipeline(monkeypatch, recorder, sid_value)
        try:
            state_register_mem.clear_session(sid_value)
            mw = make_middleware(need_update_system_prompt=False)
            req = make_request(
                [HumanMessage(content="hi"), AIMessage(content="hello")],
                session_id=sid_value,
            )
            result = mw._apply_compression(req, sid_value)

            assert recorder == []
            assert state_register_mem.get_state(sid_value, "system_prompt") is None
            assert result.system_message is None
        finally:
            try:
                state_register_mem.clear_session(sid_value)
            except Exception:
                pass

    def test_failure_propagates(self, monkeypatch):
        sid_value = "t8-hreq-fail"
        recorder = []
        self._patch_pipeline(
            monkeypatch, recorder, sid_value, raise_exc=True
        )
        try:
            state_register_mem.clear_session(sid_value)
            mw = make_middleware(need_update_system_prompt=True)
            req = make_request(
                [HumanMessage(content="hi"), AIMessage(content="hello")],
                session_id=sid_value,
            )
            with pytest.raises(RuntimeError):
                mw._apply_compression(req, sid_value)
        finally:
            try:
                state_register_mem.clear_session(sid_value)
            except Exception:
                pass


# ======================================================================
# Core methods: PART2 §9.7 reference implementation (TDD RED baseline)
# ======================================================================


class TestSummarizationCore:
    """§9.7 core helpers pinned method-by-method (chunk B).

    Red-mode classification (verified against the OLD 493-line middleware):
    - AttributeError-Red (name missing on old): _get_reported_tokens,
      _preemptive_check, _preemptive_truncate, _find_tool_name,
      _determine_cutoff, _adjust_for_orphan_pairs, _extract_previous_summary,
      _build_summary_prompt, _serialize_for_summary,
      _truncate_summary_messages.
    - Assertion-Red (exists on old via inherited SummarizationMiddleware,
      returns [SystemMessage, RemoveMessage]): _build_new_messages.
    - Behavior lock (old behaves identically — deliberate pin):
      _slice_last_turn, _get_session_or_raise, _check_last_turn_ratio
      (old _LAST_TURN_RATIO_THRESHOLD = 0.5 with >=, identical to §9.7's
      LAST_TURN_RATIO_THRESHOLD = 0.5 semantics).
    """

    # ------------------------------------------------------------------
    # _get_reported_tokens (§9.7 L954): last AIMessage usage_metadata
    # ------------------------------------------------------------------

    def test_reported_tokens_reads_last_ai_usage(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 5000,
                },
            ),
        ]
        assert mw._get_reported_tokens(msgs) == 5000

    def test_reported_tokens_zero_without_usage_metadata(self):
        # Metis boundary: no reported usage on the last AI → 0.
        mw = make_middleware()
        assert mw._get_reported_tokens([HumanMessage(content="hi")]) == 0

    def test_reported_tokens_zero_without_total_key(self):
        # pydantic validates usage_metadata at construction (total_tokens is
        # required), so inject the malformed dict via model_copy (no validation).
        mw = make_middleware()
        msg = AIMessage(content="ok").model_copy(
            update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1}}
        )
        msgs = [msg]
        assert mw._get_reported_tokens(msgs) == 0

    # ------------------------------------------------------------------
    # _preemptive_check (§9.7 L990): None / "truncate_only" / "compact"
    # Thresholds: PREEMPTIVE_TRUNCATE_RATIO=0.70, COMPRESSION_TRIGGER_RATIO=0.80
    # ------------------------------------------------------------------

    def test_preemptive_compact_at_compression_ratio(self):
        # est 800 / ctx 1000 == 0.80 >= COMPRESSION_TRIGGER_RATIO → "compact".
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [HumanMessage(content="x" * 3200)]  # est 3200 // 4 == 800
        assert mw._preemptive_check(msgs, "t8-pc-compact") == "compact"

    def test_preemptive_truncate_only_band(self):
        # est 700 / ctx 1000 == 0.70 >= PREEMPTIVE_TRUNCATE_RATIO, < 0.80.
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [HumanMessage(content="x" * 2800)]  # est 700
        assert mw._preemptive_check(msgs, "t8-pc-trunc") == "truncate_only"

    def test_preemptive_below_threshold_none(self):
        # est 70 / ctx 1000 == 0.07 → no pressure.
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [HumanMessage(content="x" * 280)]  # est 70
        assert mw._preemptive_check(msgs, "t8-pc-low") is None

    def test_preemptive_no_context_window_none(self):
        mw = make_middleware(main_llm_context_window=None)
        msgs = [HumanMessage(content="x" * 3200)]
        assert mw._preemptive_check(msgs, "t8-pc-noctx") is None

    def test_preemptive_empty_messages_none(self):
        mw = make_middleware(main_llm_context_window=1000)
        assert mw._preemptive_check([], "t8-pc-empty") is None

    # ------------------------------------------------------------------
    # _preemptive_truncate (§9.7 L1016): head/tail cap tool outputs at
    # _PREEMPTIVE_TRUNCATE_MAX_CHARS = 2000 (no LLM call)
    # ------------------------------------------------------------------

    def test_preemptive_truncates_oversized_tool_output(self):
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [
            HumanMessage(content="q"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "a.py"}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="x" * 10000, tool_call_id="call_1"),
        ]
        result = mw._preemptive_truncate(msgs, "t8-pt-big")
        assert len(result) == 3
        # Head 600 + tail 600 (2000 * 0.3 each) with omission marker.
        truncated = result[2].content
        assert "...[omitted 8800 chars]..." in truncated
        assert truncated.startswith("x" * 100)
        assert truncated.endswith("x" * 100)
        # Whole list now fits the preemptive budget (0.80 * 1000).
        assert mw._estimate_tokens(result) <= int(0.80 * 1000)

    def test_preemptive_small_list_unchanged(self):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [
            HumanMessage(content="q"),
            ToolMessage(content="small", tool_call_id="call_2"),
        ]
        result = mw._preemptive_truncate(msgs, "t8-pt-small")
        assert result[0] is msgs[0]
        assert result[1] is msgs[1]

    def test_preemptive_protected_tool_untouched(self):
        # §9.7: PROTECTED_TOOLS outputs are never preemptively truncated.
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [
            HumanMessage(content="q"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "memory", "args": {}, "id": "call_3"}
                ],
            ),
            ToolMessage(content="y" * 5000, tool_call_id="call_3"),
        ]
        result = mw._preemptive_truncate(msgs, "t8-pt-prot")
        assert result[2].content == "y" * 5000
        assert result[0] is msgs[0]  # last Human untouched
        assert result[1] is msgs[1]  # last AI untouched

    def test_preemptive_empty_messages(self):
        mw = make_middleware(main_llm_context_window=40000)
        assert mw._preemptive_truncate([], "t8-pt-empty") == []

    # ------------------------------------------------------------------
    # _find_tool_name (§9.7 L1050): resolve tool name by tool_call_id
    # ------------------------------------------------------------------

    def test_find_tool_name_resolves_from_tool_calls(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "a.py"}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="out", tool_call_id="call_1"),
        ]
        assert mw._find_tool_name(msgs, msgs[2], "call_1") == "read_file"

    def test_find_tool_name_missing_call_returns_empty(self):
        # §9.7 returns "" (not None) when no AI tool_call matches.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            AIMessage(content="plain reply"),
            ToolMessage(content="orphan", tool_call_id="call_9"),
        ]
        assert mw._find_tool_name(msgs, msgs[2], "call_9") == ""

    # ------------------------------------------------------------------
    # _slice_last_turn (§9.7 L1069): behavior lock — old middleware has
    # the identical implementation; pinned as a regression lock.
    # ------------------------------------------------------------------

    def test_slice_last_turn_returns_last_human_onward(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
        ]
        assert mw._slice_last_turn(msgs) == [msgs[2], msgs[3]]

    def test_slice_last_turn_keeps_trailing_tool_messages(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="out", tool_call_id="call_1"),
        ]
        assert mw._slice_last_turn(msgs) == [msgs[0], msgs[1], msgs[2]]

    def test_slice_last_turn_empty(self):
        mw = make_middleware()
        assert mw._slice_last_turn([]) == []

    # ------------------------------------------------------------------
    # _get_session_or_raise (§9.7 L935): behavior lock — old raises
    # RuntimeError("Not pass session_id") identically.
    # ------------------------------------------------------------------

    def test_get_session_valid_state(self):
        assert (
            summarization_module.Summarization._get_session_or_raise(
                {"session_id": "t8-x"}
            )
            == "t8-x"
        )

    def test_get_session_missing_raises(self):
        with pytest.raises(RuntimeError):
            summarization_module.Summarization._get_session_or_raise({})

    def test_get_session_none_raises(self):
        # None value → falsy-session guard path (RuntimeError per §9.7;
        # legacy shape raises AttributeError — both count as Red/lock).
        with pytest.raises((RuntimeError, AttributeError)):
            summarization_module.Summarization._get_session_or_raise(
                {"session_id": None}
            )

    # ------------------------------------------------------------------
    # _determine_cutoff (§9.7 L1167): budget-based tail selection.
    # preserve budget = clamp(ctx * 0.25, 2000..15000).
    # ------------------------------------------------------------------

    def test_determine_cutoff_zero_when_all_fit(self):
        mw = make_middleware(main_llm_context_window=40000)  # budget 10000
        msgs = [
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
        ]
        assert mw._determine_cutoff(msgs) == 0

    def test_determine_cutoff_zero_for_empty(self):
        mw = make_middleware(main_llm_context_window=40000)
        assert mw._determine_cutoff([]) == 0

    def test_determine_cutoff_exceeding_budget(self):
        # ctx 8000 → budget 2000. Turn sizes: T0=[H1,A1] est 1100+1100,
        # T1=[H2,A2] est 0. Reversed fill keeps T1 (cutoff 2); T0 overflows
        # → split_turn keeps only the suffix fitting 900 → cutoff 1.
        mw = make_middleware(main_llm_context_window=8000)
        msgs = [
            HumanMessage(content="x" * 4400),  # est 1100
            AIMessage(content="y" * 4400),  # est 1100
            HumanMessage(content="q"),
            AIMessage(content="a"),
        ]
        assert mw._determine_cutoff(msgs) == 1

    def test_determine_cutoff_never_cuts_inside_last_turn(self):
        # Single oversized turn; split would give cutoff 1 (between H and A),
        # but with _compress_last_turn False the cutoff is clamped to the
        # last HumanMessage index (0) — the last turn is never cut open.
        mw = make_middleware(main_llm_context_window=8000)
        msgs = [
            HumanMessage(content="x" * 8000),  # est 2000
            AIMessage(content="y" * 8000),  # est 2000
        ]
        assert mw._determine_cutoff(msgs) == 0

    # ------------------------------------------------------------------
    # _adjust_for_orphan_pairs (§9.7 L1197): never orphan an AI/Tool pair
    # ------------------------------------------------------------------

    def test_orphan_adjust_moves_cutoff_to_include_ai(self):
        # Cutoff 2 lands between A1(tool_calls) and T1 → moved to 1 so the
        # pair stays together in the preserved tail.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h1"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="out", tool_call_id="call_1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
        ]
        assert mw._adjust_for_orphan_pairs(msgs, 2) == 1

    def test_orphan_adjust_clean_boundary_unchanged(self):
        # Cutoff 3 → tail [H2, A2] has no orphaned ToolMessage.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h1"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="out", tool_call_id="call_1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
        ]
        assert mw._adjust_for_orphan_pairs(msgs, 3) == 3

    def test_orphan_adjust_tool_without_ai_falls_to_prev_human(self):
        # Orphan ToolMessage with no matching AI anywhere → walk back to
        # the previous HumanMessage (index 0).
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h1"),
            ToolMessage(content="orphan", tool_call_id="call_9"),
        ]
        assert mw._adjust_for_orphan_pairs(msgs, 1) == 0

    # ------------------------------------------------------------------
    # _check_last_turn_ratio (§9.7 L1080): behavior lock — old uses the
    # same threshold (0.5) and the same >= comparator. DOC WINS: §9.7
    # says ratio >= LAST_TURN_RATIO_THRESHOLD, so the spec's "boundary →
    # False (strict >)" case asserts True instead.
    # ------------------------------------------------------------------

    def test_last_turn_ratio_oversized_returns_true(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="a" * 40),  # est 10
            HumanMessage(content="b" * 4000),  # est 1000
        ]
        assert mw._check_last_turn_ratio(msgs, "t8-ratio-big") is True

    def test_last_turn_ratio_normal_returns_false(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="a" * 4000),  # est 1000
            HumanMessage(content="b" * 40),  # est 10
        ]
        assert mw._check_last_turn_ratio(msgs, "t8-ratio-small") is False

    def test_last_turn_ratio_exact_boundary_inclusive(self):
        # last est 10 / total 20 == 0.5 → >= threshold → True.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="x" * 40),  # est 10
            HumanMessage(content="y" * 40),  # est 10
        ]
        assert mw._check_last_turn_ratio(msgs, "t8-ratio-edge") is True

    # ------------------------------------------------------------------
    # _build_new_messages (§9.7 L1321): HumanMessage("What did we do
    # so far?") + AIMessage(summary, lc_source="summarization") pair.
    # Old inherited version returns [SystemMessage, RemoveMessage] →
    # all 4 cases are assertion-Red.
    # ------------------------------------------------------------------

    def test_build_new_messages_pair_framing(self):
        mw = make_middleware()
        result = mw._build_new_messages("SUMMARY-BODY-1")
        assert len(result) == 2
        assert isinstance(result[0], HumanMessage)
        assert result[0].content == "What did we do so far?"
        assert isinstance(result[1], AIMessage)
        assert result[1].additional_kwargs.get("lc_source") == "summarization"
        assert "<summary>" in result[1].content

    def test_build_new_messages_summary_text_verbatim(self):
        mw = make_middleware()
        result = mw._build_new_messages("UNIQUE-ROUNDTRIP-8f3a")
        assert "UNIQUE-ROUNDTRIP-8f3a" in result[1].content

    def test_build_new_messages_huge_summary_capped(self):
        # SUMMARY_TOTAL_MAX_CHARS = 16000: head 30% + tail 30% + marker.
        mw = make_middleware()
        result = mw._build_new_messages("Z" * 20000)
        content = result[1].content
        assert "...[summary truncated, omitted " in content
        assert len(content) < 20000

    def test_build_new_messages_fifo_completed_section(self):
        # _enforce_fifo_limits keeps only the newest COMPLETED_MAX_ITEMS
        # (5) bullets in the "### Completed" section.
        mw = make_middleware()
        summary = (
            "### Completed (most recent 5)\n"
            "- item1\n- item2\n- item3\n- item4\n"
            "- item5\n- item6\n- item7\n- item8\n"
            "\n## Next Steps\n- finish the work\n"
        )
        result = mw._build_new_messages(summary)
        content = result[1].content
        assert "(3 earlier items omitted for brevity)" in content
        assert "- item8" in content
        assert "- item1" not in content

    # ------------------------------------------------------------------
    # _extract_previous_summary (§9.7 L1233): last lc_source="summarization"
    # AIMessage, inner text of <summary>…</summary>; None when absent.
    # ------------------------------------------------------------------

    def test_extract_previous_summary_tagged_ai_message(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(
                content="intro <summary>\nPREV-TEXT-123\n</summary> outro",
                additional_kwargs={"lc_source": "summarization"},
            ),
        ]
        assert mw._extract_previous_summary(msgs) == "PREV-TEXT-123"

    def test_extract_previous_summary_absent_returns_none(self):
        # §9.7 returns None (not "") when no marked summary exists.
        mw = make_middleware()
        msgs = [HumanMessage(content="hi"), AIMessage(content="reply")]
        assert mw._extract_previous_summary(msgs) is None

    def test_extract_previous_summary_round_trips_with_build(self):
        mw = make_middleware()
        new_msgs = mw._build_new_messages("T8-ROUNDTRIP-XYZ")
        assert mw._extract_previous_summary(new_msgs) == "T8-ROUNDTRIP-XYZ"

    # ------------------------------------------------------------------
    # _build_summary_prompt (§9.7 L1252): <conversation> + optional
    # <prior-summary> + instruction literals.
    # ------------------------------------------------------------------

    def test_build_summary_prompt_with_previous(self):
        mw = make_middleware()
        prompt = mw._build_summary_prompt("T8CONV-TEXT", "T8PREV-TEXT")
        assert "Here is the conversation so far:" in prompt
        assert "<conversation>" in prompt
        assert "T8CONV-TEXT" in prompt
        assert "<prior-summary>" in prompt
        assert "T8PREV-TEXT" in prompt
        assert "updating a context checkpoint" in prompt

    def test_build_summary_prompt_first_run(self):
        mw = make_middleware()
        prompt = mw._build_summary_prompt("T8CONV-TEXT", None)
        assert "<conversation>" in prompt
        assert "T8CONV-TEXT" in prompt
        assert "creating a context checkpoint" in prompt
        assert "<prior-summary>" not in prompt

    # ------------------------------------------------------------------
    # _serialize_for_summary (§9.7 L666, module-level): role labels,
    # tool-call names, long-output truncation.
    # ------------------------------------------------------------------

    def test_serialize_role_labels(self):
        serialize = mget("_serialize_for_summary")
        out = serialize(
            [HumanMessage(content="hello"), AIMessage(content="hi there")]
        )
        assert "[User]: hello" in out
        assert "[Assistant]: hi there" in out

    def test_serialize_includes_tool_call_name(self):
        serialize = mget("_serialize_for_summary")
        out = serialize(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "a.py"},
                            "id": "call_1",
                        }
                    ],
                )
            ]
        )
        assert "[Assistant tool call]: read_file(" in out
        # Empty-content AI text line is skipped (only the tool-call line).
        assert "[Assistant]:" not in out

    def test_serialize_tool_result_truncation_marker(self):
        serialize = mget("_serialize_for_summary")
        out = serialize(
            [ToolMessage(content="x" * 3000, tool_call_id="call_9")]
        )
        assert "[Tool result] (call_9): " in out
        # 3000 chars → keep 1800 + "...[truncated 1200 chars]...".
        assert "...[truncated 1200 chars]..." in out

    def test_serialize_error_status_and_join(self):
        serialize = mget("_serialize_for_summary")
        out = serialize(
            [
                ToolMessage(
                    content="boom", tool_call_id="e1", status="error"
                ),
                HumanMessage(content="q"),
            ]
        )
        assert "[Tool error] (e1): boom" in out
        assert "\n\n" in out

    def test_serialize_deterministic(self):
        serialize = mget("_serialize_for_summary")
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "call_1"}
                ],
            ),
            ToolMessage(content="out", tool_call_id="call_1"),
        ]
        assert serialize(msgs) == serialize(msgs)

    # ------------------------------------------------------------------
    # _truncate_summary_messages (§9.7 L1456): cap lc_source-marked
    # messages at SUMMARY_TOTAL_MAX_CHARS (16000).
    # ------------------------------------------------------------------

    def test_truncate_summary_messages_caps_oversize(self):
        mw = make_middleware()
        msgs = [
            AIMessage(
                content="A" * 20000,
                additional_kwargs={"lc_source": "summarization"},
            ),
            HumanMessage(content="keep me"),
        ]
        result = mw._truncate_summary_messages(msgs)
        content = result[0].content
        # Head 4800 + tail 4800 → "...[omitted 10400 chars]...".
        assert "...[omitted 10400 chars]..." in content
        assert content.startswith("A" * 100)
        assert content.endswith("A" * 100)
        assert len(content) < 20000
        assert result[1].content == "keep me"

    def test_truncate_summary_messages_small_unchanged(self):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="h"),
            AIMessage(
                content="a", additional_kwargs={"lc_source": "summarization"}
            ),
        ]
        result = mw._truncate_summary_messages(msgs)
        assert len(result) == 2
        assert result[0].content == "h"
        assert result[1].content == "a"
        assert result[1].additional_kwargs["lc_source"] == "summarization"


# ======================================================================
# Shared markers and message builders for the middleware groups
# ======================================================================

DEDUP_MARKER_FMT = "[Duplicated call to {name} - output cleared, see latest result]"
PRUNE_MARKER = "[Old tool result content cleared]"
PREEMPTIVE_MARKER_FMT = "...[omitted {omitted} chars]..."
AGGRESSIVE_MARKER_FMT = "...[aggressively truncated, {omitted} chars omitted]"
TRUNCATE_MARKER_FMT = "...[truncated {omitted} chars]..."


def _ai_with_call(tc_id, name, args, content=""):
    return AIMessage(
        content=content, tool_calls=[{"name": name, "args": args, "id": tc_id}]
    )


def _tool(content, tc_id, status=None):
    if status is None:
        return ToolMessage(content=content, tool_call_id=tc_id)
    return ToolMessage(content=content, tool_call_id=tc_id, status=status)


def _turn(i, tool_name="search", out_chars=2000, protect=False):
    """One Human -> AI(tool call) -> Tool(result) turn."""
    name = "memory" if protect else tool_name
    tc_id = "t8-call-%d" % i
    return [
        HumanMessage(content="question %d" % i),
        _ai_with_call(tc_id, name, {"q": "query-%d" % i}),
        _tool("x" * out_chars, tc_id),
    ]


def _history(turns=6, out_chars=2000, protect=False):
    msgs = []
    for i in range(turns):
        msgs.extend(_turn(i, out_chars=out_chars, protect=protect))
    return msgs


def _summary_ai(content):
    """AIMessage flagged as a compaction summary (lc_source='summarization')."""
    return AIMessage(content=content, additional_kwargs={"lc_source": "summarization"})


def _summary_tags():
    """Resolve the Phase 2 summary tag pair (open, close) at test runtime."""
    for open_name, close_name in (
        ("_SUMMARY_PREFIX", "_SUMMARY_SUFFIX"),
        ("_SUMMARY_OPEN_TAG", "_SUMMARY_CLOSE_TAG"),
    ):
        if hasattr(summarization_module, open_name) and hasattr(
            summarization_module, close_name
        ):
            return getattr(summarization_module, open_name), getattr(
                summarization_module, close_name
            )
    pytest.fail(
        "Phase 2 summary tag constants are missing from the middleware module "
        "(expected Red against the old middleware)"
    )


def fresh_sid(prefix="t8-s"):
    """Unique session id string, defensively cleared from the mem register."""
    s = "%s-%s" % (prefix, uuid.uuid4().hex[:8])
    try:
        state_register_mem.clear_session(s)
    except Exception:
        pass
    return s


# ======================================================================
# Compression: strategy pipeline, aggressive truncate, apply modes,
# recovery context, degradation monitoring, empty-response detection
# ======================================================================


class TestSummarizationCompression:
    """Multi-strategy pipeline (dedup -> prune -> target -> aggressive),
    apply_compression modes, recovery context and degradation (PART2 s13)."""

    # ---- tool output dedup ------------------------------------------------

    def _dup_pair_msgs(self, args1, args2, out1=4000, out2=4000):
        return [
            HumanMessage(content="q1"),
            _ai_with_call("t8-d1", "search", args1),
            _tool("a" * out1, "t8-d1"),
            _ai_with_call("t8-d2", "search", args2),
            _tool("b" * out2, "t8-d2"),
            HumanMessage(content="q2"),
            AIMessage(content="done"),
        ]

    def test_dedup_marks_repeated_call(self, sid):
        mw = make_middleware()
        msgs = self._dup_pair_msgs({"q": "same"}, {"q": "same"})
        # §9.7: _run_non_llm_strategies(messages, session_id) -> (messages, int)
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result != msgs
        assert DEDUP_MARKER_FMT.format(name="search") in [
            m.content for m in result if isinstance(m.content, str)
        ]

    def test_dedup_keeps_unique_output(self, sid):
        mw = make_middleware()
        msgs = self._dup_pair_msgs({"q": "q1"}, {"q": "q2"})
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result != msgs
        assert DEDUP_MARKER_FMT.format(name="search") not in [
            m.content for m in result if isinstance(m.content, str)
        ]

    def test_dedup_budget_ok_skips_strategies(self, sid):
        # §9.7 WINS (intent conflict): dedup marks ANY older duplicate whose
        # output is longer than the ~64-char placeholder — no budget gate.
        mw = make_middleware()
        msgs = self._dup_pair_msgs({"q": "same"}, {"q": "same"}, out1=100, out2=100)
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result != msgs
        assert DEDUP_MARKER_FMT.format(name="search") in [
            m.content for m in result if isinstance(m.content, str)
        ]

    # ---- prune --------------------------------------------------------------

    def test_prune_clears_oldest_oversized_history(self, sid):
        # §9.7 WINS (intent conflict): the whole 6-turn history (~6k tokens)
        # is under PRUNE_PROTECT_TOKENS=40000, so prune never fires; target
        # truncation (largest-first, TARGET_TRUNCATE_RATIO=0.5) truncates the
        # 5 OLDEST oversized outputs and keeps the newest verbatim.
        mw = make_middleware()
        msgs = _history(turns=6, out_chars=4000)
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result != msgs
        assert result[2].content.startswith("x" * 600)
        assert "...[truncated 2800 chars]..." in result[2].content
        assert result[17].content == "x" * 4000

    def test_prune_protects_recent_under_budget(self, sid):
        # §9.7 WINS (intent conflict): total ~6k tokens << 40000 protection ->
        # prune protects everything (no prune marker); target truncation
        # still shrinks the oversized outputs.
        mw = make_middleware()
        msgs = _history(turns=6, out_chars=4000)
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result != msgs
        assert PRUNE_MARKER not in [
            m.content for m in result if isinstance(m.content, str)
        ]

    def test_protected_tools_survive_prune_and_target(self, sid):
        # §9.7 WINS (intent conflict): dedup/prune/target ALL skip protected
        # (memory) tools; aggressive truncation only exists in the
        # _apply_compression final-token check, not the gentle pipeline.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-pm-1", "memory", {"q": "x"}),
            _tool("x" * 8000, "t8-pm-1"),
        ]
        result, _ = mw._run_non_llm_strategies(msgs, sid)
        assert result == msgs

    # ---- aggressive truncate ------------------------------------------------

    def test_aggressive_truncates_protected_tool(self, sid):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-ag-1", "memory", {"q": "x"}),
            _tool("x" * 3000, "t8-ag-1"),
        ]
        result = mw._aggressive_truncate(msgs)
        assert (
            result[2].content
            == "x" * 1000 + AGGRESSIVE_MARKER_FMT.format(omitted=2000)
        )

    def test_aggressive_truncates_multiple_tools(self, sid):
        mw = make_middleware()
        msgs = [
            _ai_with_call("t8-ag-2", "search", {"q": "1"}),
            _tool("x" * 3000, "t8-ag-2"),
            _ai_with_call("t8-ag-3", "search", {"q": "2"}),
            _tool("y" * 5000, "t8-ag-3"),
        ]
        result = mw._aggressive_truncate(msgs)
        assert result[1].content == "x" * 1000 + AGGRESSIVE_MARKER_FMT.format(
            omitted=2000
        )
        assert result[3].content == "y" * 1000 + AGGRESSIVE_MARKER_FMT.format(
            omitted=4000
        )

    def test_aggressive_untouched_history_left_alone(self, sid):
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-ag-4", "search", {"q": "x"}),
            _tool("x" * 800, "t8-ag-4"),
        ]
        result = mw._aggressive_truncate(msgs)
        assert result == msgs

    # ---- apply_compression modes ---------------------------------------------

    def test_apply_compression_noop_on_small_history(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        req = make_request(msgs, session_id=sid)
        result = mw._apply_compression(req, sid)
        assert list(result.messages) == msgs
        # §9.7: _record_compression runs on EVERY apply (even noops).
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_apply_compression_truncate_only_mode(self, sid):
        # §9.7 WINS (intent conflict): no preemptive band inside
        # _apply_compression — the gentle pipeline target-truncates the
        # 3000-char output (head/tail 600 of MAX_TOOL_OUTPUT_CHARS) and the
        # apply is recorded (count 1).
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-tr-1", "search", {"q": "x"}),
            _tool("x" * 3000, "t8-tr-1"),
        ]
        req = make_request(msgs, session_id=sid)
        result = mw._apply_compression(req, sid)
        out = result.messages[2].content
        assert out.startswith("x" * 600)
        assert "...[truncated 1800 chars]..." in out
        assert len(out) == 1228
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_apply_compression_aggressive_mode(self, sid):
        # Protected memory tool 20000 chars, ctx 6000 -> compact path, all
        # gentle strategies blocked -> aggressive truncate.
        mw = make_middleware(main_llm_context_window=6000)
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-agm-1", "memory", {"q": "x"}),
            _tool("x" * 20000, "t8-agm-1"),
        ]
        req = make_request(msgs, session_id=sid)
        result = mw._apply_compression(req, sid)
        out = result.messages[2].content
        assert out.startswith("x" * 1000)
        assert AGGRESSIVE_MARKER_FMT.format(omitted=19000) in out
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_apply_compression_llm_summary_path(self, sid):
        # 100 turns of small protected tool outputs (400 chars): no strategy
        # can shrink them, est 11000 > budget 10000 -> LLM summary path.
        make_middleware(
            main_llm_context_window=40000, trigger=[("tokens", 5000)]
        )
        msgs = []
        for i in range(100):
            msgs.extend(
                [
                    HumanMessage(content="question %d" % i),
                    _ai_with_call("t8-llm-%d" % i, "memory", {"q": "x"}),
                    _tool("x" * 400, "t8-llm-%d" % i),
                ]
            )
        stub = StubModel(text="LLM wrote a deterministic and sufficiently long summary.")
        mw2 = make_middleware(
            model=stub, main_llm_context_window=40000, trigger=[("tokens", 5000)]
        )
        req = make_request(msgs, session_id=sid, model=stub)
        result = mw2._apply_compression(req, sid)
        assert len(stub.calls) >= 1
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "llm_summary"
        )
        assert len(result.messages) < len(msgs)

    def test_apply_compression_non_llm_sufficient_strategy(self, sid):
        mw = make_middleware(
            main_llm_context_window=8000, trigger=[("tokens", 500)]
        )
        msgs = self._dup_pair_msgs({"q": "same"}, {"q": "same"})
        req = make_request(msgs, session_id=sid)
        result = mw._apply_compression(req, sid)
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        # §9.7 WINS: the gentle-strategy branch keeps "non_llm" (there is no
        # separate "non_llm_sufficient" strategy value).
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "non_llm"
        )
        assert DEDUP_MARKER_FMT.format(name="search") in [
            m.content for m in result.messages if isinstance(m.content, str)
        ]

    def test_apply_compression_skip_llm_fallback(self, sid):
        mw = make_middleware(
            main_llm_context_window=8000, trigger=[("tokens", 500)]
        )
        state_register_mem.set_state(sid, mget("_SKIP_LLM_KEY"), True)
        msgs = _history(turns=6, out_chars=4000)
        req = make_request(msgs, session_id=sid)
        result = mw._apply_compression(req, sid)
        # The fallback summary is wrapped inside the summary AIMessage content.
        assert any(
            "## Latest Unresolved User Request" in m.content
            for m in result.messages
            if isinstance(m.content, str)
        )
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "fallback"
        )

    # ---- recovery context ------------------------------------------------------

    def test_capture_recovery_context_returns_error(self, sid):
        # §9.7 WINS (intent conflict): _capture_recovery_context(messages,
        # session_id) captures NO error field — it returns a dict
        # {user_intent, file_ops, previous_file_ops}.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-rc-1", "search", {"q": "x"}),
            _tool("boom", "t8-rc-1", status="error"),
        ]
        ctx = mw._capture_recovery_context(msgs, sid)
        assert isinstance(ctx, dict)
        assert ctx["user_intent"] == "q"
        assert ctx["file_ops"] == {"read_files": [], "modified_files": []}
        assert "previous_file_ops" in ctx

    def test_capture_recovery_context_no_error_none(self, sid):
        # §9.7 WINS: user_intent is the last Human text; file_ops is a dict
        # with empty lists when no file tools ran.
        mw = make_middleware()
        msgs = [HumanMessage(content="q"), AIMessage(content="ok")]
        ctx = mw._capture_recovery_context(msgs, sid)
        assert ctx["user_intent"] == "q"
        assert ctx["file_ops"] == {"read_files": [], "modified_files": []}

    def test_inject_recovery_context_creates_human_message(self, sid):
        # §9.7 WINS (intent conflict): _inject_recovery_context(messages,
        # ctx, session_id) only edits the existing summarization AIMessage —
        # injecting "## Relevant Files" before </summary> — it NEVER adds a
        # HumanMessage.
        mw = make_middleware()
        msgs = [
            HumanMessage(content="q"),
            _summary_ai("checkpoint body\n</summary>"),
        ]
        ctx = {
            "user_intent": "G",
            "file_ops": {"read_files": [], "modified_files": ["/a.py"]},
            "previous_file_ops": None,
        }
        result = mw._inject_recovery_context(msgs, ctx, sid)
        assert len(result) == len(msgs)
        assert isinstance(result[0], HumanMessage) and result[0].content == "q"
        assert "## Relevant Files" in result[1].content
        assert "/a.py" in result[1].content
        assert not state_register_mem.get_state(sid, mget("_FORCE_RECOVERY_KEY"))

    # ---- degradation monitoring -------------------------------------------------

    def test_monitor_degradation_counts_empty_responses(self, sid):
        # §9.7 WINS (intent conflict): _monitor_degradation(response,
        # session_id) returns None and is gated by _compaction_just_happened;
        # counters live in state keys. 3 empty responses (threshold) force
        # recovery and consume one attempt.
        mw = make_middleware()
        # §9.7: "empty" means a response whose str content is blank — pass an
        # AIMessage (a bare str has no .content and is NOT treated as empty).
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        assert state_register_mem.get_state(sid, mget("_DEGRADATION_NO_TEXT_KEY")) == 3
        assert state_register_mem.get_state(sid, mget("_FORCE_RECOVERY_KEY")) is True
        assert state_register_mem.get_state(sid, mget("_RECOVERY_ATTEMPTS_KEY")) == 1

    def test_monitor_degradation_resets_on_real_summary(self, sid):
        mw = make_middleware()
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content="A real summary with content."), sid)
        assert state_register_mem.get_state(sid, mget("_DEGRADATION_NO_TEXT_KEY")) == 0
        assert not state_register_mem.get_state(sid, mget("_FORCE_RECOVERY_KEY"))

    def test_monitor_degradation_caps_recovery_attempts(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_RECOVERY_ATTEMPTS_KEY"), 2)
        mw._compaction_just_happened = True
        mw._monitor_degradation(AIMessage(content=""), sid)
        assert state_register_mem.get_state(sid, mget("_RECOVERY_ATTEMPTS_KEY")) == 2
        assert not state_register_mem.get_state(sid, mget("_FORCE_RECOVERY_KEY"))

    # ---- empty response detection -------------------------------------------------

    def test_is_empty_response_blank_content_is_empty(self):
        # §9.7 WINS (intent conflict): _is_empty_response only checks for
        # blank content — there is no noop-summary pattern detection.
        assert (
            summarization_module.Summarization._is_empty_response(
                AIMessage(content="   \n\t")
            )
            is True
        )

    def test_is_empty_response_real_summary_not_empty(self):
        text = (
            "## Goal\n- fix the bug\n\n"
            "### Completed (most recent 5)\n- step one"
        )
        assert (
            summarization_module.Summarization._is_empty_response(
                AIMessage(content=text)
            )
            is False
        )

    def test_is_empty_response_empty_string_is_empty(self):
        assert (
            summarization_module.Summarization._is_empty_response(AIMessage(content=""))
            is True
        )

    # ---- skip compression (restored from 3727399 Core dedup; §9.7-verified) ----

    def test_skip_when_max_attempts_reached(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 5)
        assert mw._should_skip_compression(sid) is True

    def test_skip_false_below_thresholds(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 2)
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 0)
        assert mw._should_skip_compression(sid) is False

    def test_ineffective_switches_to_skip_llm(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 1)
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 2)
        assert mw._should_skip_compression(sid) is False
        assert state_register_mem.get_state(sid, mget("_SKIP_LLM_KEY")) is True

    def test_force_recovery_resets_counters(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_FORCE_RECOVERY_KEY"), True)
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 5)
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 2)
        assert mw._should_skip_compression(sid) is False
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 0
        assert (
            state_register_mem.get_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY")) == 0
        )
        assert not state_register_mem.get_state(sid, mget("_SKIP_LLM_KEY"))

    def test_force_recovery_checked_before_max_attempts(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_FORCE_RECOVERY_KEY"), True)
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 99)
        assert mw._should_skip_compression(sid) is False

    # ---- record compression (restored from 3727399 Core dedup; §9.7-verified) --

    def test_record_compression_increments_count(self, sid):
        mw = make_middleware()
        before = [HumanMessage(content="a"), AIMessage(content="b")]
        after = [HumanMessage(content="summary")]
        mw._record_compression(sid, before, after)
        mw._record_compression(sid, before, after)
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 2

    def test_record_compression_records_strategy(self, sid):
        mw = make_middleware()
        before = [HumanMessage(content="a"), AIMessage(content="b")]
        after = [HumanMessage(content="summary")]
        mw._record_compression(sid, before, after, strategy_used="llm_summary")
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "llm_summary"
        )

    def test_record_compression_ineffective_accumulates(self, sid):
        mw = make_middleware()
        msgs = [HumanMessage(content="a"), AIMessage(content="b")]
        mw._record_compression(sid, msgs, list(msgs))
        mw._record_compression(sid, msgs, list(msgs))
        assert (
            state_register_mem.get_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY")) == 2
        )

    def test_record_compression_effective_resets_and_unskips(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 2)
        state_register_mem.set_state(sid, mget("_SKIP_LLM_KEY"), True)
        before = [HumanMessage(content="a"), AIMessage(content="b")]
        after = [HumanMessage(content="summary")]
        mw._record_compression(sid, before, after, strategy_used="dedup")
        assert (
            state_register_mem.get_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY")) == 0
        )
        assert not state_register_mem.get_state(sid, mget("_SKIP_LLM_KEY"))


# ======================================================================
# Fallback: static summary builder, file-ops ratchet, FIFO enforcement
# ======================================================================


class TestSummarizationFallback:
    """Static fallback summary, file-operations extraction/format/parse and
    FIFO list enforcement (PART2 s13)."""

    # ---- static fallback builder ----
    # §9.7 WINS (signature): _build_static_fallback_summary(messages: list)
    # takes MESSAGES, not a SimpleNamespace ctx.

    def test_static_fallback_contains_all_sections(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build([HumanMessage(content="finish the task")])
        for section in (
            "## Latest Unresolved User Request",
            "## Goal",
            "### Completed",
            "### Blocked",
            "## Key Decisions",
            "## Next Steps",
            "## Critical Context",
            "## Relevant Files",
        ):
            assert section in text

    def test_static_fallback_goal_and_error(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build(
            [
                HumanMessage(content="finish the task"),
                ToolMessage(
                    content="tool exploded",
                    tool_call_id="t8-fe-1",
                    status="error",
                ),
            ]
        )
        assert "finish the task" in text
        assert "tool exploded" in text

    def test_static_fallback_missing_ctx_fields(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build([])
        for section in ("## Goal", "### Completed", "## Relevant Files"):
            assert section in text

    def test_static_fallback_fifo_note(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build([])
        assert "most recent" in text

    def test_static_fallback_file_ops_rendered(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build(
            [
                _ai_with_call("t8-ff-1", "write_file", {"path": "/tmp/a.py"}),
                _tool("ok", "t8-ff-1"),
            ]
        )
        assert "/tmp/a.py" in text

    def test_static_fallback_truncated_to_max_chars(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build(
            [
                HumanMessage(content="G" * 20000),
                AIMessage(content="E" * 20000),
            ]
        )
        assert len(text) <= SUMMARY_TOTAL_MAX_CHARS + 500

    def test_static_fallback_goal_capped(self, sid):
        build = mget("_build_static_fallback_summary")
        text = build([HumanMessage(content="G" * 5000)])
        assert "G" * 5000 not in text
        assert "G" * (LATEST_USER_REQUEST_MAX_CHARS + 1) not in text

    # ---- file operations extraction ----

    def test_extract_file_ops_finds_write(self, sid):
        extract = mget("_extract_file_operations")
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-fo-1", "write_file", {"path": "/tmp/x.py"}),
            _tool("written", "t8-fo-1"),
        ]
        ops = extract(msgs)
        assert "/tmp/x.py" in str(ops)

    def test_extract_file_ops_finds_read(self, sid):
        extract = mget("_extract_file_operations")
        msgs = [
            _ai_with_call("t8-fo-2", "read_file", {"path": "/tmp/y.py"}),
            _tool("contents", "t8-fo-2"),
        ]
        ops = extract(msgs)
        assert "/tmp/y.py" in str(ops)

    def test_extract_file_ops_ignores_non_file_tools(self, sid):
        extract = mget("_extract_file_operations")
        msgs = [
            _ai_with_call("t8-fo-3", "search", {"q": "x"}),
            _tool("out", "t8-fo-3"),
        ]
        ops = extract(msgs)
        assert ops == {"read_files": [], "modified_files": []}

    def test_extract_file_ops_dedupes_repeats(self, sid):
        extract = mget("_extract_file_operations")
        msgs = [
            _ai_with_call("t8-fo-4", "write_file", {"path": "/tmp/dup.py"}),
            _tool("ok", "t8-fo-4"),
            _ai_with_call("t8-fo-5", "write_file", {"path": "/tmp/dup.py"}),
            _tool("ok", "t8-fo-5"),
        ]
        ops = extract(msgs)
        assert str(ops).count("/tmp/dup.py") == 1

    def test_extract_file_ops_empty_messages(self, sid):
        extract = mget("_extract_file_operations")
        ops = extract([])
        assert ops == {"read_files": [], "modified_files": []}

    # ---- file operations formatting ----

    def test_format_file_ops_renders_paths(self, sid):
        # §9.7 WINS (signature): _format_file_ops(file_ops: dict,
        # previous=None) — renders <read-files>/<modified-files> sections,
        # never tool names.
        fmt = mget("_format_file_ops")
        text = fmt({"modified_files": ["/a.py"]})
        assert "/a.py" in text
        assert "<modified-files>" in text

    def test_format_file_ops_empty(self, sid):
        fmt = mget("_format_file_ops")
        text = fmt({})
        assert not text or "none" in str(text).lower()

    def test_format_file_ops_caps_length(self, sid):
        fmt = mget("_format_file_ops")
        ops = {"modified_files": ["/x/" + "p" * 2000]}
        assert len(fmt(ops)) <= FILE_OPS_LIST_MAX_CHARS + 200

    def test_format_file_ops_many_ops_capped(self, sid):
        fmt = mget("_format_file_ops")
        ops = {"modified_files": ["/opt%d.py" % i for i in range(50)]}
        assert len(fmt(ops)) <= FILE_OPS_LIST_MAX_CHARS + 200

    # ---- file operations parsing ----

    def test_parse_file_ops_roundtrip(self, sid):
        fmt = mget("_format_file_ops")
        parse = mget("_parse_file_ops_from_summary")
        text = fmt({"modified_files": ["/rt.py"]})
        parsed = parse(text)
        assert "/rt.py" in str(parsed)

    def test_parse_file_ops_no_section(self, sid):
        parse = mget("_parse_file_ops_from_summary")
        assert not parse("no relevant section here")

    # ---- FIFO enforcement ----

    def test_fifo_caps_completed_list(self, sid):
        fifo = mget("_enforce_fifo_limits")
        text = (
            "### Completed (most recent 5)\n"
            + "\n".join("- item%d" % i for i in range(8))
            + "\n"
        )
        out = fifo(text)
        assert "- item7" in out
        assert "- item2" not in out

    def test_fifo_caps_key_decisions(self, sid):
        fifo = mget("_enforce_fifo_limits")
        text = (
            "## Key Decisions (most recent 5)\n"
            + "\n".join("- decision%d" % i for i in range(8))
            + "\n"
        )
        out = fifo(text)
        assert "- decision7" in out
        assert "- decision2" not in out

    def test_fifo_caps_critical_context(self, sid):
        fifo = mget("_enforce_fifo_limits")
        text = (
            "## Critical Context (most recent 3)\n"
            + "\n".join("- c%d" % i for i in range(6))
            + "\n"
        )
        out = fifo(text)
        assert "- c5" in out
        assert "- c2" not in out

    def test_fifo_leaves_other_sections(self, sid):
        fifo = mget("_enforce_fifo_limits")
        text = "## Goal\n- keep me\n"
        assert "- keep me" in fifo(text)

    def test_fifo_idempotent(self, sid):
        fifo = mget("_enforce_fifo_limits")
        text = (
            "### Completed (most recent 5)\n"
            + "\n".join("- item%d" % i for i in range(8))
            + "\n"
        )
        once = fifo(text)
        assert fifo(once) == once


# ======================================================================
# Async: acreate/aapply mirrors, wrap orchestration (sync + async),
# before_agent resets, integration round-trips
# ======================================================================


class TestSummarizationAsync:
    """Async mirrors and wrap_model_call orchestration (PART2 s13)."""

    # ---- acreate_summary -------------------------------------------------

    def test_acreate_summary_returns_text(self, sid):
        # §9.7: _acreate_summary(messages_to_summarize: list) -> str
        stub = StubModel()
        mw = make_middleware(model=stub)
        text = asyncio.run(
            mw._acreate_summary([HumanMessage(content="q"), AIMessage(content="ok")])
        )
        assert len(stub.calls) >= 1
        assert stub.text in str(text)

    def test_acreate_summary_sufficient_length(self, sid):
        stub = StubModel()
        mw = make_middleware(model=stub)
        text = asyncio.run(mw._acreate_summary([HumanMessage(content="q")]))
        assert len(str(text)) >= 50

    def test_acreate_summary_error_propagates(self, sid):
        # §9.7 WINS (intent conflict): _acreate_summary catches every
        # Exception and returns the static fallback — nothing propagates.
        stub = StubModel(raise_exc=True)
        mw = make_middleware(model=stub)
        text = asyncio.run(mw._acreate_summary([HumanMessage(content="q")]))
        assert "## Latest Unresolved User Request" in str(text)

    def test_acreate_summary_empty_text_passthrough(self, sid):
        # §9.7 WINS (intent conflict): text shorter than 50 chars falls back
        # to the static fallback summary.
        # StubModel(text="") would fall back to the default long text (falsy
        # `or`), so pass a short truthy string (< 50 chars) instead.
        stub = StubModel(text="too short")
        mw = make_middleware(model=stub)
        text = asyncio.run(mw._acreate_summary([HumanMessage(content="q")]))
        assert "## Latest Unresolved User Request" in str(text)

    # ---- aapply_compression ------------------------------------------------

    def test_aapply_compression_noop_matches_sync(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        req = make_request(msgs, session_id=sid)
        result = asyncio.run(mw._aapply_compression(req, sid))
        assert list(result.messages) == msgs
        # §9.7: _record_compression runs on EVERY apply (even noops).
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_aapply_compression_llm_path_calls_ainvoke(self, sid):
        stub = StubModel(text="Async LLM wrote a deterministic long summary body.")
        mw = make_middleware(
            model=stub, main_llm_context_window=40000, trigger=[("tokens", 5000)]
        )
        msgs = []
        # 100 turns: est ~10800 > budget 10000 -> cutoff > 0 -> async LLM path.
        for i in range(100):
            msgs.extend(
                [
                    HumanMessage(content="question %d" % i),
                    _ai_with_call("t8-alm-%d" % i, "memory", {"q": "x"}),
                    _tool("x" * 400, "t8-alm-%d" % i),
                ]
            )
        req = make_request(msgs, session_id=sid, model=stub)
        result = asyncio.run(mw._aapply_compression(req, sid))
        assert len(stub.calls) >= 1
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "llm_summary"
        )
        assert len(result.messages) < len(msgs)

    def test_aapply_compression_fallback_when_skip_llm(self, sid):
        mw = make_middleware(
            main_llm_context_window=8000, trigger=[("tokens", 500)]
        )
        state_register_mem.set_state(sid, mget("_SKIP_LLM_KEY"), True)
        msgs = _history(turns=6, out_chars=4000)
        req = make_request(msgs, session_id=sid)
        result = asyncio.run(mw._aapply_compression(req, sid))
        # The fallback summary is wrapped inside the summary AIMessage content.
        assert any(
            "## Latest Unresolved User Request" in m.content
            for m in result.messages
            if isinstance(m.content, str)
        )
        assert (
            state_register_mem.get_state(sid, mget("_LAST_STRATEGY_KEY"))
            == "fallback"
        )

    def test_aapply_compression_preserves_non_summary_messages(self, sid):
        stub = StubModel(text="Async LLM wrote a deterministic long summary body.")
        mw = make_middleware(
            model=stub, main_llm_context_window=40000, trigger=[("tokens", 5000)]
        )
        msgs = []
        for i in range(50):
            msgs.extend(
                [
                    HumanMessage(content="question %d" % i),
                    _ai_with_call("t8-alp-%d" % i, "memory", {"q": "x"}),
                    _tool("x" * 400, "t8-alp-%d" % i),
                ]
            )
        req = make_request(msgs, session_id=sid, model=stub)
        result = asyncio.run(mw._aapply_compression(req, sid))
        assert any(
            isinstance(m, HumanMessage) and "question 0" in str(m.content)
            for m in result.messages
        )

    # ---- wrap_model_call (sync) ----------------------------------------------

    def test_wrap_model_call_returns_handler_result(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        req = make_request(
            [HumanMessage(content="hi"), AIMessage(content="hello")],
            session_id=sid,
        )
        assert mw.wrap_model_call(req, lambda r: "WRAP-SENTINEL") == "WRAP-SENTINEL"

    def test_wrap_model_call_small_history_no_compression(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        req = make_request(msgs, session_id=sid)
        captured = []

        def handler(r):
            captured.append(list(r.messages))
            return "WRAP-SENTINEL"

        mw.wrap_model_call(req, handler)
        assert captured[0] == msgs

    def test_wrap_model_call_truncate_mode_shrinks_tool(self, sid):
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-wt-1", "search", {"q": "x"}),
            _tool("x" * 3000, "t8-wt-1"),
        ]
        req = make_request(msgs, session_id=sid)
        captured = []

        def handler(r):
            captured.append(list(r.messages))
            return "WRAP-SENTINEL"

        mw.wrap_model_call(req, handler)
        out = captured[0][2].content
        assert out.startswith("x" * 600)
        assert PREEMPTIVE_MARKER_FMT.format(omitted=1800) in out

    def test_wrap_model_call_missing_session_id_raises(self):
        mw = make_middleware(main_llm_context_window=40000)
        req = ModelRequest(
            model=StubModel(),
            messages=[HumanMessage(content="hi")],
            state={"messages": [HumanMessage(content="hi")]},
        )
        with pytest.raises(RuntimeError):
            mw.wrap_model_call(req, lambda r: "never")

    def test_wrap_model_call_records_count_on_compact(self, sid):
        mw = make_middleware(
            main_llm_context_window=2000, trigger=[("tokens", 500)]
        )
        msgs = [
            HumanMessage(content="q1"),
            _ai_with_call("t8-wc-1", "search", {"q": "same"}),
            _tool("a" * 4000, "t8-wc-1"),
            _ai_with_call("t8-wc-2", "search", {"q": "same"}),
            _tool("b" * 4000, "t8-wc-2"),
            HumanMessage(content="q2"),
            AIMessage(content="done"),
        ]
        req = make_request(msgs, session_id=sid)
        mw.wrap_model_call(req, lambda r: "WRAP-SENTINEL")
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_wrap_model_call_handler_result_returned_on_compact(self, sid):
        mw = make_middleware(
            main_llm_context_window=2000, trigger=[("tokens", 500)]
        )
        msgs = [
            HumanMessage(content="q1"),
            _ai_with_call("t8-wc-3", "search", {"q": "same"}),
            _tool("a" * 4000, "t8-wc-3"),
            _ai_with_call("t8-wc-4", "search", {"q": "same"}),
            _tool("b" * 4000, "t8-wc-4"),
            HumanMessage(content="q2"),
            AIMessage(content="done"),
        ]
        req = make_request(msgs, session_id=sid)
        assert mw.wrap_model_call(req, lambda r: "WRAP-SENTINEL") == "WRAP-SENTINEL"

    # ---- awrap_model_call (async) ----------------------------------------------

    def test_awrap_model_call_returns_handler_result(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        req = make_request(
            [HumanMessage(content="hi"), AIMessage(content="hello")],
            session_id=sid,
        )
        async def handler(r):
            return "AWRAP-SENTINEL"

        assert asyncio.run(mw.awrap_model_call(req, handler)) == "AWRAP-SENTINEL"

    def test_awrap_model_call_small_history_no_compression(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        req = make_request(msgs, session_id=sid)
        captured = []

        async def handler(r):
            captured.append(list(r.messages))
            return "AWRAP-SENTINEL"

        asyncio.run(mw.awrap_model_call(req, handler))
        assert captured[0] == msgs

    def test_awrap_model_call_truncate_mode(self, sid):
        mw = make_middleware(main_llm_context_window=1000)
        msgs = [
            HumanMessage(content="q"),
            _ai_with_call("t8-wa-1", "search", {"q": "x"}),
            _tool("x" * 3000, "t8-wa-1"),
        ]
        req = make_request(msgs, session_id=sid)
        captured = []

        async def handler(r):
            captured.append(list(r.messages))
            return "AWRAP-SENTINEL"

        asyncio.run(mw.awrap_model_call(req, handler))
        out = captured[0][2].content
        assert PREEMPTIVE_MARKER_FMT.format(omitted=1800) in out

    def test_awrap_model_call_missing_session_id_raises(self):
        mw = make_middleware(main_llm_context_window=40000)
        req = ModelRequest(
            model=StubModel(),
            messages=[HumanMessage(content="hi")],
            state={"messages": [HumanMessage(content="hi")]},
        )
        with pytest.raises(RuntimeError):
            asyncio.run(mw.awrap_model_call(req, lambda r: "never"))

    def test_awrap_model_call_llm_path_async_stub(self, sid):
        stub = StubModel(text="Async wrap LLM deterministic long summary body here.")
        mw = make_middleware(
            model=stub, main_llm_context_window=40000, trigger=[("tokens", 5000)]
        )
        msgs = []
        # 100 turns: est ~10800 >= trigger 5000 and > budget 10000 -> cutoff
        # > 0 -> async LLM path fires (50 turns est ~5600 would stay under
        # budget and noop).
        for i in range(100):
            msgs.extend(
                [
                    HumanMessage(content="question %d" % i),
                    _ai_with_call("t8-aw-%d" % i, "memory", {"q": "x"}),
                    _tool("x" * 400, "t8-aw-%d" % i),
                ]
            )
        req = make_request(msgs, session_id=sid, model=stub)
        async def handler(r):
            return "AWRAP-SENTINEL"

        asyncio.run(mw.awrap_model_call(req, handler))
        assert len(stub.calls) >= 1
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1

    def test_awrap_model_call_fallback_skip_llm(self, sid):
        mw = make_middleware(
            main_llm_context_window=8000, trigger=[("tokens", 500)]
        )
        state_register_mem.set_state(sid, mget("_SKIP_LLM_KEY"), True)
        msgs = _history(turns=6, out_chars=4000)
        req = make_request(msgs, session_id=sid)
        captured = []

        async def handler(r):
            captured.append(list(r.messages))
            return "AWRAP-SENTINEL"

        asyncio.run(mw.awrap_model_call(req, handler))
        # The fallback summary is wrapped inside the summary AIMessage content.
        assert any(
            "## Latest Unresolved User Request" in str(m.content)
            for m in captured[0]
            if isinstance(m.content, str)
        )

    # ---- before_agent resets ------------------------------------------------

    def test_before_agent_resets_compression_state(self, sid):
        mw = make_middleware()
        state_register_mem.set_state(sid, mget("_COMPRESSION_COUNT_KEY"), 3)
        state_register_mem.set_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"), 2)
        state_register_mem.set_state(sid, mget("_SKIP_LLM_KEY"), True)
        mw._before_agent_impl({"session_id": sid})
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 0
        assert (
            state_register_mem.get_state(sid, mget("_COMPRESSION_INEFFECTIVE_KEY"))
            == 0
        )
        assert not state_register_mem.get_state(sid, mget("_SKIP_LLM_KEY"))

    def test_before_agent_no_session_key_noop(self):
        mw = make_middleware()
        # No session_id in state: must not raise.
        mw._before_agent_impl({})

    # ---- integration ----------------------------------------------------------

    def test_wrap_and_apply_integration_roundtrip(self, sid):
        mw = make_middleware(
            main_llm_context_window=2000, trigger=[("tokens", 500)]
        )
        msgs = [
            HumanMessage(content="q1"),
            _ai_with_call("t8-ir-1", "search", {"q": "same"}),
            _tool("a" * 4000, "t8-ir-1"),
            _ai_with_call("t8-ir-2", "search", {"q": "same"}),
            _tool("b" * 4000, "t8-ir-2"),
            HumanMessage(content="q2"),
            AIMessage(content="done"),
        ]
        req = make_request(msgs, session_id=sid)
        captured = []

        def handler(r):
            captured.append(list(r.messages))
            return "WRAP-SENTINEL"

        assert mw.wrap_model_call(req, handler) == "WRAP-SENTINEL"
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        assert DEDUP_MARKER_FMT.format(name="search") in [
            m.content for m in captured[0] if isinstance(m.content, str)
        ]

    def test_wrap_model_call_idempotent_noop_on_second_pass(self, sid):
        mw = make_middleware(main_llm_context_window=40000)
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        req = make_request(msgs, session_id=sid)
        mw.wrap_model_call(req, lambda r: "WRAP-SENTINEL")
        mw.wrap_model_call(req, lambda r: "WRAP-SENTINEL")
        # No compression ran: the count key is never set (get_state -> None).
        assert not state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY"))

    def test_awrap_and_before_agent_reset_between_calls(self, sid):
        mw = make_middleware(
            main_llm_context_window=2000, trigger=[("tokens", 500)]
        )
        msgs = [
            HumanMessage(content="q1"),
            _ai_with_call("t8-rb-1", "search", {"q": "same"}),
            _tool("a" * 4000, "t8-rb-1"),
            _ai_with_call("t8-rb-2", "search", {"q": "same"}),
            _tool("b" * 4000, "t8-rb-2"),
            HumanMessage(content="q2"),
            AIMessage(content="done"),
        ]
        req = make_request(msgs, session_id=sid)
        async def handler(r):
            return "AWRAP-SENTINEL"

        asyncio.run(mw.awrap_model_call(req, handler))
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 1
        mw._before_agent_impl({"session_id": sid})
        assert state_register_mem.get_state(sid, mget("_COMPRESSION_COUNT_KEY")) == 0
