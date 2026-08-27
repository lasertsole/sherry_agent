"""Unit tests for agent/middlewares/output_repetition_guard.py — OutputRepetitionGuard.

Covers:
  * Session helpers (get_session_id)
  * Hash helpers (_content_hash)
  * Internal repetition detection (_detect_* + _detect_internal_repetition)
  * Reasoning extraction (_extract_reasoning / _extract_inline_reasoning) + strip
  * AI message extraction (_extract_ai_message) across AIMessage / ModelResponse /
    ExtendedModelResponse
  * Cross-call repetition WARN / HALT escalation
  * Internal-repetition warn (single-shot) respecting the warned flag
  * Reasoning-history tracking (independent of output history)
  * Halted-turn short circuit
  * tool_calls skip path
  * before_agent / abefore_agent reset
  * wrap_model_call / awrap_model_call passthrough + replacement
  * SESSION_STATE_KEYS exposes the per-session keys for teardown cleanup
"""

import asyncio
import sys
import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---- llama_cpp is an optional runtime dependency that pulls a heavy native
# ---- DLL and isn't installed in the CI/test environment.  `import agent.*`
# ---- triggers `agent/__init__.py -> core -> models -> ITTT_model -> llama_cpp`,
# ---- which would fail on a bare run.  Stub it (and its submodule) into
# ---- sys.modules *before* any agent import so the middleware under test can be
# ---- imported in isolation.
_llama_stub = types.ModuleType("llama_cpp")
_llama_stub.Llama = type("Llama", (), {})
sys.modules.setdefault("llama_cpp", _llama_stub)
_llama_chat_sub = types.ModuleType("llama_cpp.llama_chat_format")
_llama_chat_sub.Qwen25VLChatHandler = type("Qwen25VLChatHandler", (), {})
sys.modules.setdefault("llama_cpp.llama_chat_format", _llama_chat_sub)

from runtime.core import Register
from runtime.state_register import StateRegisterMeM
from langchain_core.messages import AIMessage, SystemMessage
from langchain.agents.middleware.types import (
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)

from agent.middlewares.output_repetition_guard import (
    OutputRepetitionGuard,
    SESSION_STATE_KEYS,
    check_stream_repetition,
    _STREAM_WARNING,
    _HISTORY_KEY,
    _WARN_COUNT_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
    _MAX_HISTORY,
    _TAIL_CHARS,
    _MIN_CONTENT_LENGTH,
    _CHAR_RUN_MIN,
)


def _make_state(session_id="test-session") -> dict:
    """Create a minimal AgentState dict."""
    return {"session_id": session_id}


class TestSessionHelpers:
    def test_get_session_id(self):
        g = OutputRepetitionGuard()
        assert g._get_session_id(_make_state("s1")) == "s1"

    def test_get_session_id_empty_raises(self):
        g = OutputRepetitionGuard()
        with pytest.raises(RuntimeError, match="session_id is required"):
            g._get_session_id({"session_id": "   "})

    def test_get_session_id_missing_raises(self):
        g = OutputRepetitionGuard()
        with pytest.raises(RuntimeError, match="session_id is required"):
            g._get_session_id({})


class TestContentHash:
    def test_hash_short_content(self):
        g = OutputRepetitionGuard()
        h = g._content_hash("hello world")
        # deterministic md5 of stripped content
        import hashlib
        assert h == hashlib.md5(b"hello world").hexdigest()

    def test_hash_uses_tail_only_for_long_content(self):
        g = OutputRepetitionGuard()
        long = "x" * (_TAIL_CHARS + 100) + "TAIL_MARKER"
        h = g._content_hash(long)
        # content longer than tail -> only last _TAIL_CHARS considered
        import hashlib
        expected = hashlib.md5(long[-_TAIL_CHARS:].strip().encode()).hexdigest()
        assert h == expected
        # leading part must NOT influence the hash
        h2 = g._content_hash("DIFFERENT_PREFIX" + ("x" * (_TAIL_CHARS)) + "TAIL_MARKER")
        assert h == h2

    def test_hash_consistent_for_same_content(self):
        g = OutputRepetitionGuard()
        assert g._content_hash("abc  ") == g._content_hash("abc")  # strip whitespace


class TestInternalRepetition:
    def make(self, internal_min_lines=6, char_run_min=8):
        return OutputRepetitionGuard(internal_min_lines=internal_min_lines, char_run_min=char_run_min)

    # ---- sentence / line level ---------------------------------------
    def test_sentence_repetition_no_fire_short(self):
        g = self.make()
        assert g._detect_sentence_repetition("alpha. beta. gamma.") is False

    def test_sentence_repetition_fires_high_duplicates(self):
        g = self.make()
        content = "hello. hello. hello. hello. hello. hello. hello. hello. done."
        assert g._detect_sentence_repetition(content) is True

    def test_sentence_repetition_no_fire_distinct(self):
        g = self.make()
        content = "one. two. three. four. five. six. seven. eight."
        assert g._detect_sentence_repetition(content) is False

    def test_line_repetition_fires(self):
        g = self.make()
        content = "aaa\nbbb\naaa\nbbb\naaa\nbbb\naaa\nbbb\n"
        # 8 lines, 2 unique -> ratio 0.75 > 0.6 -> True
        assert g._detect_sentence_repetition(content) is True

    def test_internal_min_lines_threshold(self):
        g = self.make(internal_min_lines=6)
        # only 5 lines below threshold -> never fires regardless of dup ratio
        content = "same. same. same. same. same."
        assert g._detect_sentence_repetition(content) is False

    # ---- character run -------------------------------------------------
    def test_char_run_fires(self):
        g = self.make()
        assert g._detect_char_run("啊" * 8) is True

    def test_char_run_fires_above_min(self):
        g = self.make(char_run_min=5)
        assert g._detect_char_run("aaaaa") is True

    def test_char_run_below_threshold(self):
        g = self.make()
        assert g._detect_char_run("aaaaaaa") is False  # 7 < 8

    def test_char_run_skips_whitespace(self):
        g = self.make()
        # repeated spaces/newlines must not count as a char run
        assert g._detect_char_run("a " * 20) is False

    def test_char_run_mixed_with_run(self):
        g = self.make()
        assert g._detect_char_run("prefix" + "b" * 9 + "suffix") is True

    # ---- phrase periodic ------------------------------------------------
    def test_phrase_periodic_fires(self):
        g = self.make()
        content = "我来帮你" * 6
        assert g._detect_phrase_repetition(content) is True

    def test_phrase_periodic_below_repeats(self):
        g = self.make()
        # "ab" appears only 3 times, below default min_repeats=5
        content = "ab" * 3 + "xyz"
        assert g._detect_phrase_repetition(content) is False

    def test_phrase_periodic_custom_min_repeats(self):
        g = self.make()
        content = "ab" * 4 + "xyz"
        assert g._detect_phrase_repetition(content, min_repeats=3) is True

    def test_phrase_short_content_guard(self):
        g = self.make()
        assert g._detect_phrase_repetition("ab") is False  # n < 2*min_repeats

    def test_phrase_strips_whitespace_internal(self):
        g = self.make()
        # spaces inside phrase get stripped before matching
        content = "a b " * 6
        assert g._detect_phrase_repetition(content) is True

    def test_phrase_skips_blank_phrases(self):
        g = self.make()
        # pattern of spaces only -> stripped -> skipped, no crash, no fire
        assert g._detect_phrase_repetition("     ") is False

    # ---- combined detector ----------------------------------------------
    def test_internal_repetition_any_subdetector(self):
        g = self.make()
        assert g._detect_internal_repetition("啊" * 8) is True       # char run
        assert g._detect_internal_repetition("我去" * 6) is True      # phrase
        assert g._detect_internal_repetition("clean text.") is False  # none

    def test_internal_repetition_short_content_false(self):
        g = self.make()
        assert g._detect_internal_repetition("hi") is False


class TestReasoningExtraction:
    def make(self):
        return OutputRepetitionGuard()

    # ---- structured keys -----------------------------------------------
    def test_extract_reasoning_content_key(self):
        msg = AIMessage(content="answer", additional_kwargs={"reasoning_content": " chain "})
        assert OutputRepetitionGuard._extract_reasoning(msg) == "chain"

    def test_extract_reasoning_reasoning_key(self):
        msg = AIMessage(content="answer", additional_kwargs={"reasoning": " rc "})
        assert OutputRepetitionGuard._extract_reasoning(msg) == "rc"

    def test_extract_reasoning_reasoning_text_key(self):
        msg = AIMessage(content="answer", additional_kwargs={"reasoning_text": " rtx "})
        assert OutputRepetitionGuard._extract_reasoning(msg) == "rtx"

    def test_extract_reasoning_key_precedence(self):
        msg = AIMessage(
            content="a",
            additional_kwargs={"reasoning_content": "first", "reasoning": "second"},
        )
        assert OutputRepetitionGuard._extract_reasoning(msg) == "first"

    def test_extract_reasoning_no_kwargs(self):
        msg = AIMessage(content="answer")
        assert OutputRepetitionGuard._extract_reasoning(msg) == ""

    def test_extract_reasoning_malformed_kwargs(self):
        # additional_kwargs holds a non-dict -> safe fallback to empty string required
        msg = MagicMock()
        msg.additional_kwargs = "not-a-dict"
        assert OutputRepetitionGuard._extract_reasoning(msg) == ""

    def test_extract_reasoning_empty_values(self):
        msg = AIMessage(content="a", additional_kwargs={"reasoning_content": "", "reasoning": "  "})
        assert OutputRepetitionGuard._extract_reasoning(msg) == ""

    # ---- inline think patterns ------------------------------------------
    def test_extract_inline_think(self):
        c = "prefix<think>inner</think>suffix"
        assert OutputRepetitionGuard._extract_inline_reasoning(c) == "inner"

    def test_extract_inline_thinking(self):
        c = "a<thinking> raw </thinking>b"
        assert OutputRepetitionGuard._extract_inline_reasoning(c) == "raw"

    def test_extract_inline_reasoning_pattern(self):
        c = "a<reasoning> deep </reasoning>b"
        assert OutputRepetitionGuard._extract_inline_reasoning(c) == "deep"

    def test_extract_inline_multiline(self):
        c = "<think>\n line1 \n line2 \n</think>x"
        # each match group is .strip()'d individually; inner spacing preserved
        assert OutputRepetitionGuard._extract_inline_reasoning(c) == "line1 \n line2"

    def test_extract_inline_multiple_patterns(self):
        c = "<think>t1</think><reasoning>r1</reasoning><thinking>t2</thinking>"
        # iteration is pattern-outer then positions match within each all-occurrence scan,
        # so concrete order of matches is: think(t1), thinking(t2), reasoning(r1)
        assert OutputRepetitionGuard._extract_inline_reasoning(c) == "t1\nt2\nr1"

    def test_extract_inline_combined_think_and_response(self):
        # CoT wrapper like DeepSeek-R1: <thinking>...</thinking> plus normal answer
        c = "<thinking>step 1: parse</thinking> final answer here"
        assert "step 1: parse" in OutputRepetitionGuard._extract_inline_reasoning(c)

    def test_extract_inline_empty(self):
        assert OutputRepetitionGuard._extract_inline_reasoning("no tags") == ""

    def test_extract_inline_skips_empty_content(self):
        # Empty group(1) must be excluded
        assert OutputRepetitionGuard._extract_inline_reasoning("<think></think>real") == ""

    # ---- stripping -------------------------------------------------------
    def test_strip_all_inline_patterns(self):
        c = "a<think>t</think>b<reasoning>r</reasoning>c"
        assert OutputRepetitionGuard._strip_inline_reasoning(c) == "abc"

    def test_strip_no_patterns(self):
        assert OutputRepetitionGuard._strip_inline_reasoning("  hello  ") == "hello"

    def test_strip_multiline_tag(self):
        c = "x<think>\nmulti\nline\n</think>y"
        assert OutputRepetitionGuard._strip_inline_reasoning(c) == "xy"


class TestExtractAiMessage:
    def make(self):
        return OutputRepetitionGuard()

    def test_direct_ai_message(self):
        msg = AIMessage(content="hi")
        assert OutputRepetitionGuard._extract_ai_message(msg) is msg

    def test_model_response_finds_ai(self):
        msg = AIMessage(content="hi")
        resp = ModelResponse(result=[SystemMessage(content="sys"), msg])
        assert OutputRepetitionGuard._extract_ai_message(resp) is msg

    def test_model_response_no_ai(self):
        resp = ModelResponse(result=[SystemMessage(content="sys")])
        assert OutputRepetitionGuard._extract_ai_message(resp) is None

    def test_extended_model_response_recursive(self):
        msg = AIMessage(content="hi")
        inner = ModelResponse(result=[msg])
        outer = ExtendedModelResponse(model_response=inner)
        assert OutputRepetitionGuard._extract_ai_message(outer) is msg

    def test_extended_nested_deep(self):
        msg = AIMessage(content="deep")
        inner = ModelResponse(result=[msg])
        mid = ExtendedModelResponse(model_response=inner)
        outer = ExtendedModelResponse(model_response=mid)
        assert OutputRepetitionGuard._extract_ai_message(outer) is msg

    def test_non_message_ignored(self):
        assert OutputRepetitionGuard._extract_ai_message("plain string") is None


@pytest.fixture
def fresh_state(monkeypatch):
    """Provide an isolated StateRegisterMeM patched into the middleware module."""
    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]
    reg = StateRegisterMeM()
    monkeypatch.setattr("agent.middlewares.output_repetition_guard.state_register_mem", reg)
    yield reg
    # teardown: remove any state set during the test
    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]


class TestCrossCallRepetition:
    def make(self, **kw):
        return OutputRepetitionGuard(**kw)

    def _request(self, session_id="s1"):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(session_id)
        return req

    def _result(self, content, tool_calls=None, additional_kwargs=None):
        return AIMessage(
            content=content,
            tool_calls=tool_calls or [],
            additional_kwargs=additional_kwargs or {},
        )

    def test_warn_at_threshold(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        content = "A very long non-trivial response content that appears repeatedly with enough length."
        for _ in range(1):
            g._wrap_model_call_post(self._request(), self._result(content))
        # second identical -> warn
        r = g._wrap_model_call_post(self._request(), self._result(content))
        assert r is not None
        assert isinstance(r, AIMessage)
        assert "[Output Repetition Guard]" in r.content
        assert "repetition" in r.content
        # not halted yet
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is False

    def test_halt_at_max_threshold(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        content = "Another long enough repeated content that triggers the repetition detector with ample text."
        for _ in range(2):
            g._wrap_model_call_post(self._request(), self._result(content))
        # third identical -> halt
        r = g._wrap_model_call_post(self._request(), self._result(content))
        assert r is not None
        assert isinstance(r, AIMessage)
        assert "has been repeated" in r.content
        assert fresh_state.get_state("s1", _HALTED_KEY, False) is True

    def test_different_content_breaks_cross_call(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        c1 = "First long distinct output that is definitely different from the next."
        c2 = "Second long distinct output that is definitely different from the first."
        c3 = "Third completely unrelated output, different from both prior answers here."
        g._wrap_model_call_post(self._request(), self._result(c1))
        g._wrap_model_call_post(self._request(), self._result(c2))
        # consecutive identical streak is broken -> no warn/no halt
        r = g._wrap_model_call_post(self._request(), self._result(c3))
        assert r is None
        # a further repeat of the newest output should warn again (counter reset)
        r = g._wrap_model_call_post(self._request(), self._result(c3))
        assert r is not None
        assert isinstance(r, AIMessage)
        assert "[Output Repetition Guard]" in r.content

    def test_history_trimming_at_max(self, fresh_state):
        g = self.make(max_identical_outputs=3)
        # push more than _MAX_HISTORY entries of distinct content
        for i in range(_MAX_HISTORY + 5):
            g._content_hash(f"content-{i}")  # warm nothing
            g._wrap_model_call_post(
                self._request(), self._result(f"Distinct content entry number {i} with padding text.")
            )
        hist = fresh_state.get_state("s1", _HISTORY_KEY, [])
        assert len(hist) <= _MAX_HISTORY

    def test_short_content_below_min_skipped(self, fresh_state):
        g = self.make()
        short = "hi"
        # not long enough for _MIN_CONTENT_LENGTH -> no history recorded, no warn
        g._wrap_model_call_post(self._request(), self._result(short))
        assert fresh_state.get_state("s1", _HISTORY_KEY, []) == []

    def test_separate_sessions_independent(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        content = "Session-scoped long repeated content used to prove isolation between sessions."
        g._wrap_model_call_post(self._request("A"), self._result(content))
        g._wrap_model_call_post(self._request("A"), self._result(content))
        # session B starts fresh -> no warn
        r = g._wrap_model_call_post(self._request("B"), self._result(content))
        assert r is None

    def test_tool_call_skips_guard(self, fresh_state):
        g = self.make(warn_after=1, max_identical_outputs=2)
        content = "I will call a tool now with enough long text to be meaningful."
        msg = self._result(content, tool_calls=[{"name": "web_search", "args": {}, "id": "c1"}])
        r = g._wrap_model_call_post(self._request(), msg)
        assert r is None  # no history recorded for tool-call messages
        assert fresh_state.get_state("s1", _HISTORY_KEY, []) == []


class TestInternalWarn:
    def make(self, **kw):
        return OutputRepetitionGuard(**kw)

    def _request(self, session_id="s1"):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(session_id)
        return req

    def _result(self, content, tool_calls=None):
        return AIMessage(content=content, tool_calls=tool_calls or [])

    def test_internal_repetition_warns_once(self, fresh_state):
        g = self.make(warn_after=5, max_identical_outputs=10)  # no cross-call trigger
        # > _MIN_CONTENT_LENGTH(20) so the internal detector runs; char run triggers it
        repetitive = "啊" * 40
        r1 = g._wrap_model_call_post(self._request(), self._result(repetitive))
        assert r1 is not None
        assert "highly repetitive" in r1.content
        # second time: already warned -> not warned again
        r2 = g._wrap_model_call_post(
            self._request(), AIMessage(content="normal distinct text that is different.", tool_calls=[])
        )
        # distinct content won't warn, but more importantly the flag is set
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is True

    def test_internal_warn_not_raised_again_next_turn(self, fresh_state):
        g = self.make(warn_after=5, max_identical_outputs=10)
        repetitive = "字" * 40
        g._wrap_model_call_post(self._request(), self._result(repetitive))
        # Even a fresh repetitive pass: flag already true -> no extra AIMessage
        r = g._wrap_model_call_post(self._request(), self._result(repetitive))
        assert r is None


class TestReasoningHistory:
    def make(self, **kw):
        return OutputRepetitionGuard(**kw)

    def _request(self, session_id="s1"):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(session_id)
        return req

    def _reasoned_msg(self, content, reasoning):
        return AIMessage(content=content, additional_kwargs={"reasoning_content": reasoning})

    def test_reasoning_repetition_tracked_independently(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        # same reasoning across calls, DIFFERENT visible content
        reasoning = "repeated reasoning chain that stays identical across multiple calls here"
        for i in range(2):
            g._wrap_model_call_post(
                self._request(),
                self._reasoned_msg(f"visible answer number {i} - differs each call", reasoning),
            )
        hist = fresh_state.get_state("s1", _REASONING_HISTORY_KEY, [])
        assert len(hist) == 2  # reasoning history is separate from output history

    def test_reasoning_warn_escalation(self, fresh_state):
        g = self.make(warn_after=2, max_identical_outputs=3)
        reasoning = "stuck reasoning loop being repeated again and again verbatim"
        # Two identical reasoning in a row -> warn (reasoning path)
        r = None
        for i in range(2):
            r = g._wrap_model_call_post(
                self._request(), self._reasoned_msg(f"visible answer for iteration {i}", reasoning)
            )
        assert r is not None
        assert "repetition" in r.content

    def test_reasoning_short_skipped(self, fresh_state):
        g = self.make()
        reasoning = "abc"
        g._wrap_model_call_post(self._request(), self._reasoned_msg("long enough visible output", reasoning))
        # below _MIN_CONTENT_LENGTH -> not tracked in reasoning history
        assert fresh_state.get_state("s1", _REASONING_HISTORY_KEY, []) == []


class TestHaltedTurn:
    def make(self, **kw):
        return OutputRepetitionGuard(**kw)

    def _request(self, session_id="s1"):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(session_id)
        return req

    def test_halted_short_circuit_returns_halt(self, fresh_state):
        g = self.make()
        fresh_state.set_state("s1", _HALTED_KEY, True)
        r = g._wrap_model_call_post(self._request(), AIMessage(content="padded content text"))
        assert r is not None
        assert "must stop" in r.content


class TestHooks:
    def make(self, **kw):
        return OutputRepetitionGuard(**kw)

    def _request(self, session_id="s1"):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(session_id)
        return req

    def _result(self, content, tool_calls=None):
        return AIMessage(content=content, tool_calls=tool_calls or [])

    def test_before_agent_resets_state(self, fresh_state):
        g = self.make()
        # Seed some stale state
        fresh_state.set_state("s1", _HISTORY_KEY, ["hash"])
        fresh_state.set_state("s1", _WARN_COUNT_KEY, 7)
        fresh_state.set_state("s1", _INTERNAL_WARNED_KEY, True)
        fresh_state.set_state("s1", _HALTED_KEY, True)
        fresh_state.set_state("s1", _REASONING_HISTORY_KEY, ["rhash"])
        fresh_state.set_state("s1", _REASONING_WARNED_KEY, True)

        runtime = MagicMock()
        g.before_agent(_make_state("s1"), runtime)

        assert fresh_state.get_state("s1", _HISTORY_KEY, "MISSING") == []
        assert fresh_state.get_state("s1", _WARN_COUNT_KEY, "MISSING") == 0
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, "MISSING") is False
        assert fresh_state.get_state("s1", _HALTED_KEY, "MISSING") is False
        assert fresh_state.get_state("s1", _REASONING_HISTORY_KEY, "MISSING") == []
        assert fresh_state.get_state("s1", _REASONING_WARNED_KEY, "MISSING") is False

    def test_abefore_agent_resets_state(self, fresh_state):
        g = self.make()
        asyncio.run(g.abefore_agent(_make_state("s1"), MagicMock()))
        assert fresh_state.get_state("s1", _HISTORY_KEY, "MISSING") == []

    def test_session_state_keys_exposed(self, fresh_state):
        # The teardown path deletes exactly the keys this middleware owns.
        assert SESSION_STATE_KEYS == (
            _HISTORY_KEY,
            _WARN_COUNT_KEY,
            _INTERNAL_WARNED_KEY,
            _HALTED_KEY,
            _REASONING_HISTORY_KEY,
            _REASONING_WARNED_KEY,
        )

    def test_teardown_delete_state_releases_only_owned_keys(self, fresh_state):
        # Simulate the subagent teardown cleanup in spawn/core.py: delete exactly
        # the 6 repetition keys from the child's session bucket while leaving the
        # rest of the bucket (other middlewares' top-level state) intact.
        fresh_state.set_state("s1", _HISTORY_KEY, ["hash"])
        fresh_state.set_state("s1", _WARN_COUNT_KEY, 7)
        fresh_state.set_state("s1", _INTERNAL_WARNED_KEY, True)
        fresh_state.set_state("s1", _HALTED_KEY, True)
        fresh_state.set_state("s1", _REASONING_HISTORY_KEY, ["rhash"])
        fresh_state.set_state("s1", _REASONING_WARNED_KEY, True)
        # Unrelated per-session state from other middlewares must survive.
        fresh_state.set_state("s1", "heartbeat_killed", True)
        fresh_state.set_state("s1", "summarization_window", "summary")

        for key in SESSION_STATE_KEYS:
            fresh_state.delete_state("s1", key)

        # The 6 owned keys are gone.
        for key in SESSION_STATE_KEYS:
            assert fresh_state.get_state("s1", key, "MISSING") == "MISSING"
        # The rest of the bucket is preserved.
        assert fresh_state.get_state("s1", "heartbeat_killed", "MISSING") is True
        assert fresh_state.get_state("s1", "summarization_window", "MISSING") == "summary"
        # The session bucket itself still exists (delete_state, not clear_session).
        assert fresh_state.has_session("s1") is True

    def test_wrap_model_call_passthrough_when_clean(self, fresh_state):
        g = self.make()
        request = self._request("s1")
        ok_response = ModelResponse(result=[AIMessage(content="a normal single output text here")])
        handler = MagicMock(return_value=ok_response)
        out = g.wrap_model_call(request, handler)
        handler.assert_called_once()
        assert out is ok_response

    def test_wrap_model_call_replacement_on_warn(self, fresh_state):
        g = self.make(warn_after=1, max_identical_outputs=3)
        request = self._request("s1")
        content = "Repeated content that is long enough to be tracked across consecutive calls."
        handler = MagicMock(return_value=ModelResponse(result=[AIMessage(content=content)]))
        g.wrap_model_call(request, handler)
        out = g.wrap_model_call(request, handler)
        assert isinstance(out, AIMessage)
        assert "repetition" in out.content

    def test_awrap_model_call_passthrough(self, fresh_state):
        async def run():
            g = self.make()
            request = self._request("s1")
            ok_response = ModelResponse(result=[AIMessage(content="async clean response text here")])
            handler = AsyncMock(return_value=ok_response)
            out = await g.awrap_model_call(request, handler)
            handler.assert_awaited_once()
            return out, ok_response
        out, ok_response = asyncio.run(run())
        assert out is ok_response  # clean response passes through unchanged

    def test_awrap_model_call_replacement(self, fresh_state):
        async def run():
            g = self.make(warn_after=1, max_identical_outputs=3)
            request = self._request("s1")
            content = "Async repeated content long enough to be tracked across consecutive calls."
            resp = ModelResponse(result=[AIMessage(content=content)])
            handler = AsyncMock(return_value=resp)
            await g.awrap_model_call(request, handler)  # call 1: records
            out = await g.awrap_model_call(request, handler)  # call 2: identical -> warn
            return out
        out = asyncio.run(run())
        assert isinstance(out, AIMessage)
        assert "repetition" in out.content


class TestConfigAndConstants:
    def test_init_defaults(self):
        g = OutputRepetitionGuard()
        assert g.max_identical_outputs == 3
        assert g.warn_after == 2
        assert g.internal_repeat_ratio == 0.6
        assert g.internal_min_lines == 6
        assert g.char_run_min == _CHAR_RUN_MIN

    def test_init_custom(self):
        g = OutputRepetitionGuard(
            max_identical_outputs=5,
            warn_after=3,
            internal_repeat_ratio=0.8,
            internal_min_lines=10,
            char_run_min=4,
        )
        assert g.max_identical_outputs == 5
        assert g.warn_after == 3
        assert g.internal_repeat_ratio == 0.8
        assert g.internal_min_lines == 10
        assert g.char_run_min == 4

    def test_is_agent_middleware(self):
        from langchain.agents.middleware import AgentMiddleware
        assert issubclass(OutputRepetitionGuard, AgentMiddleware)

    def test_state_keys_constants(self):
        assert _HISTORY_KEY == "output_repetition_history"
        assert _WARN_COUNT_KEY == "output_repetition_warn_count"
        assert _INTERNAL_WARNED_KEY == "output_repetition_internal_warned"
        assert _HALTED_KEY == "output_repetition_halted"
        assert _REASONING_HISTORY_KEY == "output_repetition_reasoning_history"
        assert _REASONING_WARNED_KEY == "output_repetition_reasoning_warned"


class TestStreamRepetition:
    """Layer C — module-level ``check_stream_repetition`` (stream-level guard).

    Used by ``async_generate`` / ``resume_agent`` to cut a repetitive tail mid-stream.
    Reuses the middleware's internal-repetition detector and the per-session
    ``_INTERNAL_WARNED_KEY`` dedupe so a session warns at most once across both the
    stream path and the ``wrap_model_call`` backstop.
    """

    def test_clean_long_text_returns_none(self, fresh_state):
        # Long, distinct text has no repetitive pattern -> never warns.
        clean = "This is a perfectly normal and varied answer that says many distinct things about the topic and avoids repeating any single phrase or character run at all in this long output."
        assert check_stream_repetition("s1", clean) is None
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is False

    def test_char_run_fires_once_per_session(self, fresh_state):
        # 40 identical CJK chars is a char-run (> char_run_min=8) and > 20 length.
        repetitive = "字" * 40
        warning = check_stream_repetition("s1", repetitive)
        assert warning is not None
        assert warning == _STREAM_WARNING
        assert "[Output Repetition Guard]" in warning and "highly repetitive" in warning
        # Deduped on the same session: second call returns None despite repetition.
        assert check_stream_repetition("s1", repetitive) is None
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is True

    def test_phrase_repetition_fires(self, fresh_state):
        # 8 repeats of a 4-char phrase (> min_repeats=5) and > 20 chars total.
        repetitive = "我来帮你" * 8
        assert check_stream_repetition("s1", repetitive) is not None
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is True

    def test_below_length_gate_returns_none(self, fresh_state):
        # Even a pure char-run is gated below _MIN_CONTENT_LENGTH=20 to mirror
        # the post-hoc middleware path (no new false-positive surface).
        short_run = "啊" * 10  # 10 < 20
        assert check_stream_repetition("s1", short_run) is None
        # And it must NOT have consumed the session's single warning.
        assert check_stream_repetition("s1", "啊" * 40) is not None

    def test_sessions_independent(self, fresh_state):
        repetitive = "字" * 40
        assert check_stream_repetition("sA", repetitive) is not None
        # Session B starts fresh -> warns independently.
        assert check_stream_repetition("sB", repetitive) is not None
        assert fresh_state.get_state("sA", _INTERNAL_WARNED_KEY, False) is True
        assert fresh_state.get_state("sB", _INTERNAL_WARNED_KEY, False) is True
        # A already warned -> once per session, so third call on sA returns None.
        assert check_stream_repetition("sA", repetitive) is None

    def test_warn_flag_reset_allows_rearming(self, fresh_state):
        # before_agent resets the flag each turn. After reset, a fresh repetitive
        # stream can warn again (dedupe is turn-scoped, not lifetime-scoped).
        repetitive = "字" * 40
        assert check_stream_repetition("s1", repetitive) is not None
        # Simulate the middleware's per-turn reset (see TestHooks).
        guard = OutputRepetitionGuard()
        guard.before_agent(_make_state("s1"), MagicMock())
        assert fresh_state.get_state("s1", _INTERNAL_WARNED_KEY, False) is False
        assert check_stream_repetition("s1", repetitive) is not None


class TestNoSessionStateCleanup:
    """Ensures guards do not leak history across distinct sessions via the real register."""

    def test_session_isolation(self, fresh_state):
        g = OutputRepetitionGuard(warn_after=2, max_identical_outputs=3)
        content = "Isolating long repeated text between separate sessions fully here."
        g._wrap_model_call_post(self._req("s1"), AIMessage(content=content))
        g._wrap_model_call_post(self._req("s1"), AIMessage(content=content))
        # s2 unaffected
        r = g._wrap_model_call_post(self._req("s2"), AIMessage(content=content))
        assert r is None

    @staticmethod
    def _req(sid):
        req = MagicMock(spec=ModelRequest)
        req.state = _make_state(sid)
        return req
