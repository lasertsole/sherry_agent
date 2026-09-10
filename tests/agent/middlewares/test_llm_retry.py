"""Tests for LLMRetryMiddleware — retry loop, circuit breaker, content filter, fallback."""

import asyncio

import pytest
from langchain.agents.middleware.types import ModelRequest

from agent.middlewares.llm_retry import (
    ContentFilterError,
    FallbackCandidate,
    LLMRetryConfig,
    LLMRetryMiddleware,
)
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-llmretry-1"


class _SentinelModel:
    def __init__(self, name: str) -> None:
        self.model_name = name


def _request(model=_SentinelModel("main")) -> ModelRequest:
    return ModelRequest(
        model=model,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        messages=[],
        state={"session_id": SID},
    )


def _ok_result(req: ModelRequest) -> str:
    return f"ok:{getattr(req.model, 'model_name', '?')}"


def _zero_backoff(monkeypatch) -> None:
    monkeypatch.setattr("agent.middlewares.llm_retry.jittered_backoff", lambda attempt, **kw: 0.0)


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


def _sync_middleware(**config) -> LLMRetryMiddleware:
    return LLMRetryMiddleware(config=LLMRetryConfig(**config))


# ---- retry-on-transient ----------------------------------------------------


class TestRetryOnTransient:
    def test_retries_then_succeeds_on_timeout(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert calls["n"] == 3

    def test_retries_then_succeeds_on_500(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=2)
        exc = type("InternalServerError", (Exception,), {"status_code": 500})("boom")
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise exc
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert calls["n"] == 2

    def test_exhausted_retries_reraise_original(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=2)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 3  # initial + 2 retries

    def test_async_retries_then_succeeds(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=2)
        calls = {"n": 0}

        async def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return _ok_result(req)

        assert asyncio.run(mw.awrap_model_call(_request(), handler)) == "ok:main"
        assert calls["n"] == 2


class TestNoRetryOnDeterministic:
    def _assert_no_retry(self, exc_cls, message):
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise exc_cls(message)

        with pytest.raises(Exception, match=message):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 1

    def test_content_policy_blocked_never_retries(self):
        self._assert_no_retry(Exception, "content_policy_violation: rejected")

    def test_ssl_never_retries(self):
        self._assert_no_retry(Exception, "certificate verify failed")

    def test_format_error_never_retries(self):
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise type("BadRequest", (Exception,), {"status_code": 400})("bad json")

        with pytest.raises(Exception, match="bad json"):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 1

    def test_context_overflow_reraises_for_summarization(self):
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise Exception("This model's maximum context length is 65536 tokens")

        with pytest.raises(Exception, match="maximum context length"):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 1


# ---- circuit breaker -------------------------------------------------------


class TestStaleStreakBreaker:
    def test_trips_at_threshold_before_next_attempt(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3, stale_giveup_threshold=3)

        def handler(req):
            raise TimeoutError("timed out")

        # The breaker is checked before EACH attempt: three in-turn timeout
        # retries bump the streak to the threshold, so attempt 4 aborts.
        with pytest.raises(RuntimeError, match="Provider unresponsive"):
            mw.wrap_model_call(_request(), handler)
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 3

    def test_cross_turn_trips_immediately(self):
        mw = _sync_middleware(stale_giveup_threshold=3)
        state_register_mem.set_state(SID, "llm_stale_streak", 3)

        def handler(req):
            raise AssertionError("handler must not run once the breaker tripped")

        with pytest.raises(RuntimeError, match="Provider unresponsive"):
            mw.wrap_model_call(_request(), handler)

    def test_resets_on_success(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3, stale_giveup_threshold=5)
        state_register_mem.set_state(SID, "llm_stale_streak", 3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 0

    def test_non_timeout_failures_do_not_bump(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=1)

        def handler(req):
            raise type("InternalServerError", (Exception,), {"status_code": 500})("boom")

        with pytest.raises(Exception, match="boom"):
            mw.wrap_model_call(_request(), handler)
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 0


# ---- content-filter flag ----------------------------------------------------


class TestContentFilterFlag:
    def test_flag_consumed_on_success_without_fallback_raises(self):
        mw = _sync_middleware()
        state_register_mem.set_state(SID, "llm_content_filter_blocked", True)

        def handler(req):
            return _ok_result(req)

        with pytest.raises(ContentFilterError, match="safety refusal"):
            mw.wrap_model_call(_request(), handler)
        assert state_register_mem.get_state(SID, "llm_content_filter_blocked", False) is False

    def test_flag_consumed_on_success_with_fallback_switches_model(self):
        mw = LLMRetryMiddleware(
            fallback_chain=[FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        )
        state_register_mem.set_state(SID, "llm_content_filter_blocked", True)
        seen = []

        def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            if seen[-1] == "fb1":
                return _ok_result(req)
            raise Exception("model not found")

        assert mw.wrap_model_call(_request(), handler) == "ok:fb1"
        assert seen == ["main", "fb1"]

    def test_flag_consumed_after_exception_raises_content_filter(self):
        mw = _sync_middleware()
        state_register_mem.set_state(SID, "llm_content_filter_blocked", True)

        def handler(req):
            raise TimeoutError("stream died mid-flight")

        with pytest.raises(ContentFilterError):
            mw.wrap_model_call(_request(), handler)


# ---- partial-stream stub -----------------------------------------------------


class TestPartialStreamStub:
    def _set_stub(self, cause: str = "timeout") -> None:
        state_register_mem.set_state(SID, "llm_partial_stream_stub", True)
        state_register_mem.set_state(SID, "llm_partial_stream_cause", cause)

    def test_stub_flag_discards_result_and_retries(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        self._set_stub("timeout")
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        # first result discarded (cut response), one fresh retry
        assert calls["n"] == 2
        assert state_register_mem.get_state(SID, "llm_partial_stream_stub", False) is False
        assert state_register_mem.get_state(SID, "llm_partial_stream_cause", "") == ""

    def test_stub_without_cause_defaults_to_timeout(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3, stale_giveup_threshold=99)
        state_register_mem.set_state(SID, "llm_partial_stream_stub", True)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 2
        # timeout classification bumps the stale streak
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 1

    def test_unknown_cause_falls_back_to_timeout(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        self._set_stub("not-a-reason")
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert calls["n"] == 2

    def test_stub_exhausted_keeps_current_result(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=0)
        self._set_stub("server_error")
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert calls["n"] == 1

    def test_async_stub_retries(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        self._set_stub("timeout")
        calls = {"n": 0}

        async def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        assert asyncio.run(mw.awrap_model_call(_request(), handler)) == "ok:main"
        assert calls["n"] == 2

    def test_stub_retry_strips_callbacks_on_stream_turns(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        self._set_stub("timeout")
        state_register_mem.set_state(SID, "is_stream_turn", True)
        sentinel = object()
        seen = []

        class _Req:
            def __init__(self):
                self.state = {"session_id": SID}
                self.config = {"callbacks": sentinel}
                self.model_settings = {}
                self.model = _SentinelModel("main")

        def handler(req):
            seen.append(req.config.get("callbacks"))
            return _ok_result(req)

        req = _Req()
        mw.wrap_model_call(req, handler)
        assert seen[0] is sentinel
        assert seen[1] is None  # stripped during the stub re-call
        assert req.config.get("callbacks") is sentinel  # restored after

    def test_stub_never_reaches_max_tokens_boost(self, monkeypatch):
        """Chain-order guard: boost wraps retry; a stub-flagged result is
        converted to a retry inside the retry middleware, so the boost layer
        only ever sees a clean (non-truncated) result."""
        from agent.middlewares.max_tokens_boost import MaxTokensBoostMiddleware

        _zero_backoff(monkeypatch)
        retry = _sync_middleware(max_retries=3)
        boost = MaxTokensBoostMiddleware()
        self._set_stub("timeout")
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok_result(req)

        req = _request()
        result = boost.wrap_model_call(req, lambda r: retry.wrap_model_call(r, handler))
        assert result == "ok:main"
        assert calls["n"] == 2
        assert "max_tokens" not in req.model_settings


# ---- content-filter terminated flag ------------------------------------------


class TestContentFilterTerminated:
    def test_terminated_flag_switches_to_fallback(self):
        mw = LLMRetryMiddleware(
            fallback_chain=[FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        )
        state_register_mem.set_state(SID, "llm_content_filter_terminated", True)
        seen = []

        def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:fb1"
        assert seen == ["main", "fb1"]
        assert state_register_mem.get_state(SID, "llm_content_filter_terminated", False) is False

    def test_terminated_flag_without_chain_raises_content_filter(self):
        mw = _sync_middleware()
        state_register_mem.set_state(SID, "llm_content_filter_terminated", True)

        def handler(req):
            return _ok_result(req)

        with pytest.raises(ContentFilterError, match="safety refusal"):
            mw.wrap_model_call(_request(), handler)

    def test_either_flag_triggers_single_fallback(self):
        mw = LLMRetryMiddleware(
            fallback_chain=[FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        )
        state_register_mem.set_state(SID, "llm_content_filter_blocked", True)
        state_register_mem.set_state(SID, "llm_content_filter_terminated", True)
        seen = []

        def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:fb1"
        assert seen == ["main", "fb1"]


# ---- fallback chain ---------------------------------------------------------


class TestFallbackChain:
    def _chain(self) -> list[FallbackCandidate]:
        return [
            FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1")),
            FallbackCandidate("moonshot", "kimi", _SentinelModel("fb2")),
        ]

    def test_non_retryable_fallback_switches_model_and_resets_retries(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = LLMRetryMiddleware(fallback_chain=self._chain())
        seen = []
        attempts_per_model: dict[str, int] = {}

        def handler(req):
            name = getattr(req.model, "model_name", "?")
            seen.append(name)
            attempts_per_model[name] = attempts_per_model.get(name, 0) + 1
            if name == "main":
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            if name == "fb1":
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:fb2"
        assert attempts_per_model == {"main": 1, "fb1": 1, "fb2": 1}

    def test_chain_exhausted_reraises_original(self):
        mw = LLMRetryMiddleware(fallback_chain=self._chain())

        def handler(req):
            raise type("PermissionDeniedError", (Exception,), {})("denied")

        with pytest.raises(Exception, match="denied"):
            mw.wrap_model_call(_request(), handler)
        assert state_register_mem.get_state(SID, "llm_fallback_index", 0) == 2

    def test_sticky_index_reapplies_active_candidate_next_call(self):
        mw = LLMRetryMiddleware(fallback_chain=self._chain())
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        seen = []

        def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:fb1"
        assert seen == ["fb1"]

    def test_empty_chain_is_plain_retry(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=2)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] < 2:
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            return _ok_result(req)

        with pytest.raises(Exception, match="denied"):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 1  # non-retryable without chain: single attempt

    def test_missing_session_id_passes_through(self):
        mw = _sync_middleware(max_retries=2)
        req = ModelRequest(
            model=_SentinelModel("main"),  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
            messages=[],
            state={},
        )
        calls = {"n": 0}

        def handler(r):
            calls["n"] += 1
            return "ok"

        assert mw.wrap_model_call(req, handler) == "ok"
        assert calls["n"] == 1

    def test_async_fallback_and_breaker(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = LLMRetryMiddleware(fallback_chain=self._chain(), config=LLMRetryConfig(max_retries=1))
        seen = []

        async def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            if seen[-1] == "main":
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            return _ok_result(req)

        assert asyncio.run(mw.awrap_model_call(_request(), handler)) == "ok:fb1"
        assert seen == ["main", "fb1"]

        state_register_mem.set_state(SID, "llm_stale_streak", 5)

        async def dead(req):
            return "never"

        with pytest.raises(RuntimeError, match="Provider unresponsive"):
            asyncio.run(mw.awrap_model_call(_request(), dead))
