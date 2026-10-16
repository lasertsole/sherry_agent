"""Unit tests for agent/middlewares/invalid_tool_call_repair/.

Covers:
  * ``repair_tool_args`` — the conservative syntax-only repair: both real
    samples from the SWE-bench HARD run (``evals/swebench/RESULTS.md``,
    run 20261017_003529), trailing commas, and every refusal rule
    (ambiguous middle value, key-like tokens inside the value, JSON-token
    starts, non-mappings, non-strings).
  * ``InvalidToolCallRepairMiddleware`` — passthrough on clean messages,
    sibling preservation with repaired calls reattached (ids kept), truncation
    left to MaxTokensBoost, the bounded resend (recovery, plain-answer
    acceptance, budget exhaustion), and the stream-turn callback stripping.
"""

import sys
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage

# ---- llama_cpp is an optional runtime dependency that pulls a heavy native
# ---- DLL and isn't installed in the CI/test environment. Stub it before any
# ---- agent import (same convention as test_output_repetition_guard.py).
_llama_stub = types.ModuleType("llama_cpp")
_llama_stub.Llama = type("Llama", (), {})
sys.modules.setdefault("llama_cpp", _llama_stub)
_llama_chat_sub = types.ModuleType("llama_cpp.llama_chat_format")
_llama_chat_sub.Qwen25VLChatHandler = type("Qwen25VLChatHandler", (), {})
sys.modules.setdefault("llama_cpp.llama_chat_format", _llama_chat_sub)

from unittest.mock import MagicMock

from langchain.agents.middleware.types import ModelRequest
from runtime.session.state_keys import StateKey

from agent.middlewares.invalid_tool_call_repair import (
    InvalidToolCallRepairMiddleware,
    repair_tool_args,
)

pytestmark = [pytest.mark.unit]

#: The two REAL invalid ``terminal`` argument strings from the django run
#: (checkpoint ``sandbox/src/checkpoints/sqlite.db``, run 20261017_003529).
DJANGO_SAMPLE_1 = '{"commands": cd $(pwd) && pwd && git log --oneline -3 && ls}'
DJANGO_SAMPLE_2 = (
    '{"commands": cd /repo 2>/dev/null || cd .; git log --oneline -3; '
    'python -c "import django; print(django.__version__)"}'
)


class TestRepairToolArgs:
    def test_repairs_real_django_sample_1(self):
        assert repair_tool_args(DJANGO_SAMPLE_1) == {
            "commands": "cd $(pwd) && pwd && git log --oneline -3 && ls"
        }

    def test_repairs_real_django_sample_2_inner_quotes(self):
        assert repair_tool_args(DJANGO_SAMPLE_2) == {
            "commands": "cd /repo 2>/dev/null || cd .; git log --oneline -3; "
            'python -c "import django; print(django.__version__)"'
        }

    def test_repairs_trailing_comma_and_keeps_string_commas(self):
        # The comma INSIDE the string value survives; only the syntactic
        # trailing comma is dropped.
        assert repair_tool_args('{"a": 1, "b": "x, y }",}') == {"a": 1, "b": "x, y }"}

    def test_rejects_ambiguous_middle_value(self):
        # An unquoted value followed by another member: the cut point is
        # ambiguous, so the repair must refuse.
        assert repair_tool_args('{"a": 1, "b": xyz, "c": 2}') is None

    def test_rejects_key_like_token_inside_the_tail(self):
        assert repair_tool_args('{"a": 1, "b": cmd "c": 2}') is None

    def test_rejects_json_token_start(self):
        # Quoting a nested/structural value would change its TYPE, not its syntax.
        assert repair_tool_args('{"a": [1, 2}') is None
        assert repair_tool_args('{"a": True}') is None

    def test_rejects_nested_head_keys(self):
        assert repair_tool_args('{"a": {"b": 1}, "c": xyz}') is None

    def test_leaves_valid_json_alone(self):
        assert repair_tool_args('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_rejects_non_mapping_and_non_string(self):
        assert repair_tool_args("[1, 2]") is None
        assert repair_tool_args("42") is None
        assert repair_tool_args(None) is None
        assert repair_tool_args("") is None
        assert repair_tool_args("   ") is None

    def test_rejects_unclosed_object(self):
        assert repair_tool_args('{"commands": cd /repo && git l') is None


def _state(session_id="s1"):
    return {"session_id": session_id, "messages": []}


def _request(session_id="s1", config=None):
    req = MagicMock(spec=ModelRequest)
    req.state = _state(session_id)
    req.messages = [HumanMessage(content="do the thing")]
    req.config = config if config is not None else {}
    req.override.side_effect = lambda **kw: _request(session_id, config)
    return req


def _invalid_ai(
    content="",
    *,
    name="terminal",
    call_id="call_1",
    raw=DJANGO_SAMPLE_1,
    error=None,
    tool_calls=None,
    finish_reason=None,
):
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": call_id, "name": name, "args": raw, "error": error}
        ],
        response_metadata={"finish_reason": finish_reason} if finish_reason else {},
    )


class TestRepairStage:
    def test_clean_message_passthrough(self):
        mw = InvalidToolCallRepairMiddleware()
        ai = AIMessage(content="hello", tool_calls=[])
        result = mw.wrap_model_call(_request(), lambda _req: ai)
        assert result is ai

    def test_valid_sibling_preserved_and_invalid_repaired(self):
        mw = InvalidToolCallRepairMiddleware()
        sibling = {
            "name": "search_files",
            "args": {"pattern": "x"},
            "id": "call_0",
            "type": "tool_call",
        }
        ai = _invalid_ai(content="let me check", tool_calls=[sibling])
        handler = MagicMock(return_value=ai)
        result = mw.wrap_model_call(_request(), handler)

        assert isinstance(result, AIMessage)
        assert result is not ai
        assert [c["id"] for c in result.tool_calls] == ["call_0", "call_1"]
        assert result.tool_calls[1]["name"] == "terminal"
        assert result.tool_calls[1]["args"] == {
            "commands": "cd $(pwd) && pwd && git log --oneline -3 && ls"
        }
        assert result.invalid_tool_calls == []
        assert result.content == "let me check"

    def test_unrepairable_kept_and_nothing_recovered(self):
        mw = InvalidToolCallRepairMiddleware()
        ai = _invalid_ai(raw='{"a": 1, "b": xyz, "c": 2}')
        handler = MagicMock(return_value=ai)
        result = mw.wrap_model_call(_request(), handler)
        # Nothing repairable and the resend cannot do better here (the mock
        # returns the same invalid message), so the ORIGINAL result is what
        # the loop ends up with — today's behavior, unchanged.
        assert result is ai

    def test_mixed_repaired_and_unrepairable(self):
        mw = InvalidToolCallRepairMiddleware()
        ai = AIMessage(
            content="",
            tool_calls=[],
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "id": "call_good",
                    "name": "terminal",
                    "args": DJANGO_SAMPLE_1,
                    "error": None,
                },
                {
                    "type": "invalid_tool_call",
                    "id": "call_bad",
                    "name": "terminal",
                    "args": '{"a": 1, "b": xyz, "c": 2}',
                    "error": None,
                },
            ],
        )
        result = mw.wrap_model_call(_request(), lambda _req: ai)
        assert isinstance(result, AIMessage)
        assert [c["id"] for c in result.tool_calls] == ["call_good"]
        assert [c["id"] for c in result.invalid_tool_calls] == ["call_bad"]

    def test_truncation_defers_to_max_tokens_boost(self):
        mw = InvalidToolCallRepairMiddleware()
        ai = _invalid_ai(finish_reason="length")
        result = mw.wrap_model_call(_request(), lambda _req: ai)
        assert result is ai
        assert ai.invalid_tool_calls  # untouched

    def test_missing_id_not_repaired(self):
        mw = InvalidToolCallRepairMiddleware()
        ai = _invalid_ai(call_id=None)
        result = mw.wrap_model_call(_request(), lambda _req: ai)
        assert result is ai


class TestResendStage:
    def test_resend_recovers_with_a_valid_call(self):
        mw = InvalidToolCallRepairMiddleware()
        bad = _invalid_ai(raw='{"a": 1, "b": xyz, "c": 2}')
        good = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "terminal",
                    "args": {"commands": "ls"},
                    "id": "call_9",
                    "type": "tool_call",
                }
            ],
        )
        calls = []

        def handler(req):
            calls.append(req)
            return bad if len(calls) == 1 else good

        request = _request()
        result = mw.wrap_model_call(request, handler)
        assert result is good
        # The corrective note rode on the RETRY request only (the middleware
        # builds it through request.override — never into state).
        assert request.override.called
        passed = request.override.call_args.kwargs["messages"]
        assert any(
            isinstance(m, HumanMessage) and "not valid JSON" in str(m.content) for m in passed
        )

    def test_resend_accepts_a_plain_answer(self):
        mw = InvalidToolCallRepairMiddleware()
        bad = _invalid_ai(raw='{"a": 1, "b": xyz, "c": 2}')
        plain = AIMessage(content="Done — no further action needed.")
        calls = []

        def handler(req):
            calls.append(req)
            return bad if len(calls) == 1 else plain

        result = mw.wrap_model_call(_request(), handler)
        assert result is plain

    def test_resend_budget_exhausted_returns_original(self):
        mw = InvalidToolCallRepairMiddleware()
        bad = _invalid_ai(raw='{"a": 1, "b": xyz, "c": 2}')
        calls = []

        def handler(req):
            calls.append(req)
            return bad

        result = mw.wrap_model_call(_request(), handler)
        assert result is bad
        # 1 initial + resend_max_retries (2) attempts.
        assert len(calls) == 3

    def test_stream_turn_strips_callbacks_for_the_retry(self):
        import asyncio

        from runtime import state_register_mem

        mw = InvalidToolCallRepairMiddleware()
        bad = _invalid_ai(raw='{"a": 1, "b": xyz, "c": 2}')
        good = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "terminal",
                    "args": {"commands": "ls"},
                    "id": "call_9",
                    "type": "tool_call",
                }
            ],
        )
        sentinel = object()
        config = {"callbacks": sentinel}
        request = _request(config=config)
        state_register_mem.set_state("s1", StateKey.IS_STREAM_TURN, True)
        seen = {}
        calls = 0

        async def handler(_req):
            nonlocal calls
            calls += 1
            if calls == 1:
                return bad
            seen["callbacks_in_flight"] = config.get("callbacks")
            return good

        try:
            result = asyncio.run(mw.awrap_model_call(request, handler))
        finally:
            state_register_mem.set_state("s1", StateKey.IS_STREAM_TURN, "")

        assert result is good
        # Stripped while the retry ran (the first attempt's tokens already went
        # out on the wire), restored afterwards.
        assert seen["callbacks_in_flight"] is None
        assert config["callbacks"] is sentinel
