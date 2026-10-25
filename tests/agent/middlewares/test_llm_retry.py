"""Tests for LLMRetryMiddleware — retry loop, circuit breaker, content filter, fallback."""

import asyncio
import base64
import io

import pytest
from PIL import Image
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares import llm_capability_cache
from agent.middlewares.llm_retry.core import (
    ContentFilterError,
    FallbackCandidate,
    LLMRetryConfig,
    LLMRetryMiddleware,
    _detect_media_types_in_messages,
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
    monkeypatch.setattr(
        "agent.middlewares.llm_retry.core.compute_retry_delay", lambda attempt, **kw: 0.0
    )


@pytest.fixture(autouse=True)
def _clean_session():
    yield
    state_register_mem.clear_session(SID)


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    llm_capability_cache.reset_cache()
    yield
    llm_capability_cache.reset_cache()


@pytest.fixture(autouse=True)
def _fallback_src_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.middlewares.media_pipeline.fallback.SRC_DIR", tmp_path / "src")


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

    def test_turn_scope_recovers_a_tripped_streak(self):
        mw = _sync_middleware(stale_giveup_threshold=5)
        state_register_mem.set_state(SID, "llm_stale_streak", 5)

        with pytest.raises(RuntimeError, match="Provider unresponsive"):
            mw.wrap_model_call(_request(), lambda req: _ok_result(req))

        mw.before_agent({"session_id": SID})

        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 0
        assert mw.wrap_model_call(_request(), lambda req: _ok_result(req)) == "ok:main"

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
        mw = LLMRetryMiddleware(
            fallback_chain=self._chain(),
            config=LLMRetryConfig(max_fallback_switches=2),
        )
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
        mw = LLMRetryMiddleware(
            fallback_chain=self._chain(),
            config=LLMRetryConfig(max_fallback_switches=2),
        )

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

    def test_default_cap_allows_only_one_switch_per_turn(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = LLMRetryMiddleware(fallback_chain=self._chain())
        seen: list[str] = []

        def handler(req):
            name = getattr(req.model, "model_name", "?")
            seen.append(name)
            if name in ("main", "fb1"):
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            return _ok_result(req)

        with pytest.raises(Exception, match="denied"):
            mw.wrap_model_call(_request(), handler)

        assert seen == ["main", "fb1"]
        assert state_register_mem.get_state(SID, "llm_fallback_index", 0) == 1
        assert state_register_mem.get_state(SID, "llm_fallback_switches", 0) == 1

    def test_zero_cap_ignores_a_sticky_candidate(self):
        mw = LLMRetryMiddleware(
            fallback_chain=self._chain(),
            config=LLMRetryConfig(max_fallback_switches=0),
        )
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        seen: list[str] = []

        def handler(req):
            seen.append(getattr(req.model, "model_name", "?"))
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert seen == ["main"]

    def test_zero_cap_blocks_a_new_switch(self):
        mw = LLMRetryMiddleware(
            fallback_chain=self._chain(),
            config=LLMRetryConfig(max_fallback_switches=0),
        )

        def handler(req):
            raise type("PermissionDeniedError", (Exception,), {})("denied")

        with pytest.raises(Exception, match="denied"):
            mw.wrap_model_call(_request(), handler)
        assert state_register_mem.get_state(SID, "llm_fallback_index", 0) == 0
        assert state_register_mem.get_state(SID, "llm_fallback_switches", 0) == 0

    def test_before_agent_resets_the_per_turn_switch_counter(self):
        mw = _sync_middleware()
        state_register_mem.set_state(SID, "llm_fallback_switches", 1)
        state_register_mem.set_state(SID, "llm_stale_streak", 3)

        mw.before_agent({"session_id": SID})

        assert state_register_mem.get_state(SID, "llm_fallback_switches", 0) == 0
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 0

    def test_before_agent_session_scope_keeps_the_streak(self):
        mw = _sync_middleware(stale_giveup_scope="session")
        state_register_mem.set_state(SID, "llm_stale_streak", 3)

        mw.before_agent({"session_id": SID})

        assert state_register_mem.get_state(SID, "llm_stale_streak", 3) == 3

    def test_abefore_agent_resets_the_same_counters(self):
        mw = _sync_middleware()
        state_register_mem.set_state(SID, "llm_fallback_switches", 1)
        state_register_mem.set_state(SID, "llm_stale_streak", 4)

        asyncio.run(mw.abefore_agent({"session_id": SID}))

        assert state_register_mem.get_state(SID, "llm_fallback_switches", 0) == 0
        assert state_register_mem.get_state(SID, "llm_stale_streak", 0) == 0

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


# ---- native multimodal → skill fallback -------------------------------------

TRYING_KEY = "_multimodal_trying_native"
MODEL_KEY = "_multimodal_native_model"
MODEL_KEY_VALUE = "testprov/testmodel"


def _png_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(1, 2, 3)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _media_message() -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_base64()}"}},
        ]
    )


def _media_request() -> ModelRequest:
    return ModelRequest(
        model=_SentinelModel("main"),  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        messages=[_media_message()],
        state={"session_id": SID},
    )


def _multimodal_error() -> Exception:
    return type("BadRequest", (Exception,), {"status_code": 400})("unsupported content type")


class TestMultimodalFallback:
    def _arm_attempt(self, model_key: str = MODEL_KEY_VALUE) -> None:
        state_register_mem.set_state(SID, TRYING_KEY, True)
        state_register_mem.set_state(SID, MODEL_KEY, model_key)

    def test_rejection_writes_cache_and_retries_on_skill_path(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=0)
        self._arm_attempt()
        seen: list = []
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            seen.append(req.messages[-1].content)
            if calls["n"] == 1:
                raise _multimodal_error()
            return "ok"

        assert mw.wrap_model_call(_media_request(), handler) == "ok"
        assert calls["n"] == 2  # fallback retried despite max_retries=0
        assert all(item["type"] == "text" for item in seen[1])
        assert "image_to_text" in seen[1][0]["text"]
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_cache_write_covers_every_media_family_present(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=0)
        self._arm_attempt()
        calls = {"n": 0}
        req = ModelRequest(
            model=_SentinelModel("main"),  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
            messages=[
                HumanMessage(
                    content=[
                        {"type": "text", "text": "混合"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        {"type": "video_bytes", "video_bytes": b"\x00\x00\x00\x18ftypmp42"},
                    ]
                )
            ],
            state={"session_id": SID},
        )

        def handler(r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _multimodal_error()
            return "ok"

        assert mw.wrap_model_call(req, handler) == "ok"
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "video") == "unsupported"
        )
        assert llm_capability_cache.get_capability("testprov", "testmodel", "audio") == "auto"

    def test_without_active_attempt_rejection_is_reraised_without_retry(self):
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise _multimodal_error()

        with pytest.raises(Exception, match="unsupported content type"):
            mw.wrap_model_call(_media_request(), handler)
        assert calls["n"] == 1
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"

    def test_active_attempt_without_media_is_not_retried(self):
        mw = _sync_middleware(max_retries=3)
        self._arm_attempt()
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise _multimodal_error()

        with pytest.raises(Exception, match="unsupported content type"):
            mw.wrap_model_call(_request(), handler)
        assert calls["n"] == 1
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_non_multimodal_error_leaves_cache_and_flag_untouched(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=0)
        self._arm_attempt()

        def handler(req):
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            mw.wrap_model_call(_media_request(), handler)
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is True

    def test_async_rejection_falls_back(self, monkeypatch):
        _zero_backoff(monkeypatch)
        mw = _sync_middleware(max_retries=0)
        self._arm_attempt()
        calls = {"n": 0}

        async def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _multimodal_error()
            return "ok"

        assert asyncio.run(mw.awrap_model_call(_media_request(), handler)) == "ok"
        assert calls["n"] == 2
        assert (
            llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "unsupported"
        )

    def test_detect_media_types_in_messages(self):
        messages = [
            HumanMessage(content="plain"),
            AIMessage(
                content=[
                    {"type": "text", "text": "x"},
                    {"type": "image_url", "image_url": {"url": "u"}},
                    {"type": "audio_bytes", "audio_bytes": b"a"},
                    "not-a-dict",
                ]
            ),
        ]

        assert _detect_media_types_in_messages(messages) == {"vision", "audio"}

    def test_detect_media_types_in_messages_without_media(self):
        assert _detect_media_types_in_messages([HumanMessage(content="plain")]) == set()

    def test_rejection_on_a_fallback_candidate_still_rewrites_to_skill_path(self, monkeypatch):
        """A sticky fallback candidate that rejects the media blocks gets the
        same skill-path rewrite — the fallback stays the serving model."""
        _zero_backoff(monkeypatch)
        chain = [FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        mw = LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0), fallback_chain=chain)
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        self._arm_attempt()
        seen_models: list[str] = []
        seen_content: list = []
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            seen_models.append(getattr(req.model, "model_name", "?"))
            seen_content.append(req.messages[-1].content)
            if calls["n"] == 1:
                raise _multimodal_error()
            return "ok"

        assert mw.wrap_model_call(_media_request(), handler) == "ok"
        assert calls["n"] == 2
        assert seen_models == ["fb1", "fb1"]
        assert [item["type"] for item in seen_content[0]] == ["text", "image_url"]
        assert [item["type"] for item in seen_content[1]] == ["text"]
        assert "image_to_text" in seen_content[1][0]["text"]

    def test_sticky_fallback_rejection_is_attributed_to_candidate(self, monkeypatch):
        """§2.2 scenario 2 (sticky path): the env main model is natively
        capable, but a fallback candidate serves the call and rejects the media
        — the rejection is cached against the candidate, never the main model."""
        _zero_backoff(monkeypatch)
        chain = [FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        mw = LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0), fallback_chain=chain)
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        self._arm_attempt()  # MultimodalProcessor recorded the env main model key
        seen_models: list[str] = []
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            seen_models.append(getattr(req.model, "model_name", "?"))
            if calls["n"] == 1:
                raise _multimodal_error()
            return "ok"

        assert mw.wrap_model_call(_media_request(), handler) == "ok"
        assert calls["n"] == 2  # skill-path retry despite max_retries=0
        assert seen_models == ["fb1", "fb1"]
        assert (
            llm_capability_cache.get_capability("deepseek", "deepseek-chat", "vision")
            == "unsupported"
        )
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert state_register_mem.get_state(SID, MODEL_KEY, "") == "deepseek/deepseek-chat"
        assert state_register_mem.get_state(SID, TRYING_KEY, False) is False

    def test_switching_candidate_rejection_is_attributed_to_candidate(self, monkeypatch):
        """§2.2 scenario 2 (_try_fallback path): the main model fails with a
        fallback-classified error mid-loop, the candidate is activated, then the
        candidate rejects the media — the cache lands on the candidate."""
        _zero_backoff(monkeypatch)
        chain = [
            FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1")),
            FallbackCandidate("moonshot", "kimi", _SentinelModel("fb2")),
        ]
        mw = LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0), fallback_chain=chain)
        self._arm_attempt()
        seen_models: list[str] = []
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            name = getattr(req.model, "model_name", "?")
            seen_models.append(name)
            if name == "main":
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            if calls["n"] == 2:
                raise _multimodal_error()
            return "ok"

        assert mw.wrap_model_call(_media_request(), handler) == "ok"
        assert calls["n"] == 3
        assert seen_models == ["main", "fb1", "fb1"]
        assert (
            llm_capability_cache.get_capability("deepseek", "deepseek-chat", "vision")
            == "unsupported"
        )
        assert llm_capability_cache.get_capability("testprov", "testmodel", "vision") == "auto"
        assert llm_capability_cache.get_capability("moonshot", "kimi", "vision") == "auto"
        assert state_register_mem.get_state(SID, "llm_fallback_index", 0) == 1

    def test_rebind_without_native_attempt_writes_no_native_model_key(self):
        """Rebinding to a fallback candidate outside a native attempt must not
        create the per-turn native-model key (no superfluous state writes)."""
        chain = [FallbackCandidate("deepseek", "deepseek-chat", _SentinelModel("fb1"))]
        mw = LLMRetryMiddleware(config=LLMRetryConfig(max_retries=0), fallback_chain=chain)
        state_register_mem.delete_state(SID, MODEL_KEY)
        state_register_mem.delete_state(SID, TRYING_KEY)

        # sticky rebind path
        state_register_mem.set_state(SID, "llm_fallback_index", 1)
        assert mw.wrap_model_call(_request(), lambda req: "ok") == "ok"
        assert state_register_mem.has_key(SID, MODEL_KEY) is False

        # switching rebind path
        state_register_mem.set_state(SID, "llm_fallback_index", 0)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise type("PermissionDeniedError", (Exception,), {})("denied")
            return "ok"

        assert mw.wrap_model_call(_request(), handler) == "ok"
        assert calls["n"] == 2
        assert state_register_mem.has_key(SID, MODEL_KEY) is False


# ---- ZCode-parity retry progress / Retry-After / empty completion ----------


class TestRetryProgress:
    def _events(self, monkeypatch):
        events: list[dict] = []

        def capture(session_id, **kw):
            events.append({"session_id": session_id, **kw})

        monkeypatch.setattr("agent.middlewares.llm_retry.core.publish_model_retry", capture)
        return events

    def test_scheduled_and_succeeded_events_carry_attempt_counts(self, monkeypatch):
        _zero_backoff(monkeypatch)
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=3)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"

        assert [event["status"] for event in events] == ["scheduled", "succeeded"]
        assert events[0]["attempt"] == 2
        assert events[0]["budget_attempt"] == 2
        assert events[0]["max_attempts"] == 4
        assert events[0]["retries"] == 1
        assert events[0]["extra_attempts"] == 0
        assert events[0]["fallback_switches"] == 0
        assert events[0]["reason"] == "timeout"
        assert events[1]["retries"] == 1

    def test_exhaustion_event_is_pushed_before_reraise(self, monkeypatch):
        _zero_backoff(monkeypatch)
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=1)

        def handler(req):
            raise type("InternalServerError", (Exception,), {"status_code": 500})("boom")

        with pytest.raises(Exception, match="boom"):
            mw.wrap_model_call(_request(), handler)

        assert [event["status"] for event in events] == ["scheduled", "exhausted"]
        assert events[-1]["attempt"] == 2
        assert events[-1]["budget_attempt"] == 2
        assert events[-1]["max_attempts"] == 2
        assert events[-1]["retries"] == 1

    def test_retry_after_header_sets_the_delay(self, monkeypatch):
        events = self._events(monkeypatch)
        sleeps: list[float] = []
        monkeypatch.setattr("agent.middlewares.llm_retry.core.time.sleep", sleeps.append)
        mw = _sync_middleware(max_retries=1, respect_retry_after=True)
        calls = {"n": 0}

        class RateLimitedError(Exception):
            status_code = 429
            headers = {"Retry-After": "7"}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitedError("slow down")
            return _ok_result(req)

        assert mw.wrap_model_call(_request(), handler) == "ok:main"
        assert sleeps == [7.0]
        assert events[0]["delay_ms"] == 7000

    def test_empty_completion_retries_once(self, monkeypatch):
        _zero_backoff(monkeypatch)
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=3, empty_completion_retries=1)
        results = [AIMessage(content=""), AIMessage(content="answered")]

        def handler(req):
            return results.pop(0)

        assert mw.wrap_model_call(_request(), handler).content == "answered"
        assert [event["reason"] for event in events] == ["empty_completion", "empty_completion"]
        assert [event["status"] for event in events] == ["scheduled", "succeeded"]

    def test_empty_completion_budget_exhaustion_returns_the_empty_result(self, monkeypatch):
        _zero_backoff(monkeypatch)
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=3, empty_completion_retries=1)

        def handler(req):
            return AIMessage(content="")

        assert mw.wrap_model_call(_request(), handler).content == ""
        assert [event["status"] for event in events] == ["scheduled", "exhausted"]
        assert [event["attempt"] for event in events] == [2, 2]
        assert [event["budget_attempt"] for event in events] == [2, 2]
        assert events[-1]["retries"] == 1
        assert events[-1]["reason"] == "empty_completion"

    def test_extra_attempts_shift_only_the_physical_attempt(self, monkeypatch):
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=10)

        mw._emit_retry(
            SID,
            "scheduled",
            1,
            "rate_limit",
            1.0,
            physical_attempt=3,
            extra_attempts=1,
        )

        event = events[0]
        assert event["attempt"] == 3
        assert event["budget_attempt"] == 2
        assert event["max_attempts"] == 12
        assert event["retries"] == 1
        assert event["extra_attempts"] == 1

    def test_event_carries_the_per_turn_fallback_switches(self, monkeypatch):
        events = self._events(monkeypatch)
        mw = _sync_middleware(max_retries=10)
        state_register_mem.set_state(SID, "llm_fallback_switches", 1)

        mw._emit_retry(SID, "scheduled", 1, "rate_limit", 1.0, physical_attempt=2)

        assert events[0]["fallback_switches"] == 1


def test_empty_completion_without_retry_budget_is_untouched(monkeypatch):
    _zero_backoff(monkeypatch)
    events = []
    monkeypatch.setattr(
        "agent.middlewares.llm_retry.core.publish_model_retry",
        lambda session_id, **kw: events.append(kw),
    )
    mw = _sync_middleware(max_retries=3, empty_completion_retries=0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return AIMessage(content="")

    assert mw.wrap_model_call(_request(), handler).content == ""
    assert calls["n"] == 1
    assert events == []
