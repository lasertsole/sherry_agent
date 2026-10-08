"""ThinkingControlMiddleware — per-session thinking control on model calls.

The middleware swaps ``request.model`` for the thinking variant matching the
session's stored flag: boolean on/off, or a low/high/max level for always-think
models. Unset/ambiguous flags pass through untouched (env default), variant
build failures fail open, and a gateway rejection of the disable payload
ladders down to the minimum-thinking variant exactly once.
"""

import asyncio

import pytest
from langchain.agents.middleware import ModelRequest

import models
from agent.middlewares.thinking_control import ThinkingControlMiddleware
from runtime import state_register_mem

pytestmark = [pytest.mark.unit]

_FLAG_KEY = "llm_thinking_enabled"


class _FakeModel:
    def __init__(self, tag: str):
        self.tag = tag


@pytest.fixture(autouse=True)
def fake_build(monkeypatch):
    """Stub build_main_llm so no real LLM client is constructed."""
    calls: list[dict] = []

    def _fake_build(temperature=None, *, thinking=None, thinking_level=None, thinking_floor=False):
        calls.append({"thinking": thinking, "level": thinking_level, "floor": thinking_floor})
        tag = (
            f"level:{thinking_level}"
            if thinking_level
            else f"thinking={thinking}(floor={thinking_floor})"
        )
        return _FakeModel(tag)

    monkeypatch.setattr(models, "build_main_llm", _fake_build)
    return calls


@pytest.fixture
def sid():
    s = "thinking-test-1"
    yield s
    state_register_mem.clear_session(s)


def _make_request(model=_FakeModel("base"), session_id="thinking-test-1"):
    return ModelRequest(model=model, messages=[], state={"session_id": session_id})


def _run(mw, request):
    """Drive both wrap paths with handlers that record the received model."""
    seen: list = []

    def sync_handler(r):
        seen.append(r.model)
        return "ok-sync"

    async def async_handler(r):
        seen.append(r.model)
        return "ok-async"

    sync_out = mw.wrap_model_call(request, sync_handler)
    async_out = asyncio.run(mw.awrap_model_call(request, async_handler))
    return sync_out, async_out, seen


def test_unset_flag_passes_request_through(sid):
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _, _, seen = _run(mw, request)
    assert all(m is request.model for m in seen)


def test_non_flag_values_pass_through(sid):
    for junk in ("true", 1, "off", "medium"):
        state_register_mem.set_state(sid, _FLAG_KEY, junk)
        mw = ThinkingControlMiddleware()
        request = _make_request()
        _, _, seen = _run(mw, request)
        assert all(m is request.model for m in seen), junk


def test_bool_flag_swaps_model_both_paths(sid):
    state_register_mem.set_state(sid, _FLAG_KEY, True)
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _, _, seen = _run(mw, request)
    assert all(m.tag == "thinking=True(floor=False)" for m in seen)

    state_register_mem.set_state(sid, _FLAG_KEY, False)
    _, _, seen = _run(mw, request)
    assert all(m.tag == "thinking=False(floor=False)" for m in seen)


def test_level_flag_swaps_level_variant(sid):
    state_register_mem.set_state(sid, _FLAG_KEY, "low")
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _, _, seen = _run(mw, request)
    assert all(m.tag == "level:low" for m in seen)


def test_variant_cache_reuses_instances(sid, fake_build):
    state_register_mem.set_state(sid, _FLAG_KEY, True)
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _run(mw, request)
    _run(mw, request)
    assert fake_build.count({"thinking": True, "level": None, "floor": False}) == 1

    state_register_mem.set_state(sid, _FLAG_KEY, "max")
    _run(mw, request)
    assert fake_build[-1] == {"thinking": None, "level": "max", "floor": False}


def test_flag_removal_returns_to_env_default(sid):
    state_register_mem.set_state(sid, _FLAG_KEY, True)
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _run(mw, request)
    state_register_mem.clear_session(sid)
    _, _, seen = _run(mw, request)
    assert all(m is request.model for m in seen)


def test_build_failure_fails_open(sid, monkeypatch):
    state_register_mem.set_state(sid, _FLAG_KEY, False)

    def _boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(models, "build_main_llm", _boom)
    mw = ThinkingControlMiddleware()
    request = _make_request()
    _, _, seen = _run(mw, request)
    assert all(m is request.model for m in seen)


def test_parked_choice_does_not_affect_the_running_turn(sid, fake_build, fake_profile_build):
    """A mid-turn switch parks under the *_pending keys; the live keys rule.

    The middleware reads the live values only, so the running turn keeps its
    model/variant until turn_runner promotes the parked choice.
    """
    state_register_mem.set_state(sid, "llm_thinking_enabled_pending", {"value": True})
    state_register_mem.set_state(sid, "llm_main_model_pending", {"value": _PROFILE})
    mw = ThinkingControlMiddleware()
    request = _make_request()

    _, _, seen = _run(mw, request)

    assert all(m is request.model for m in seen), "parked choices must not swap mid-turn"
    assert fake_build == [] and fake_profile_build == []


def test_no_session_id_passthrough():
    mw = ThinkingControlMiddleware()
    request = ModelRequest(model=_FakeModel("base"), messages=[], state={})
    _, _, seen = _run(mw, request)
    assert all(m is request.model for m in seen)


class _AlwaysThinkRejectionError(RuntimeError):
    def __str__(self):
        return "400 该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"


def test_disable_rejection_ladders_to_floor_once(sid, fake_build):
    """The disable payload 400s → one floor retry; later offs skip the rung."""
    state_register_mem.set_state(sid, _FLAG_KEY, False)
    mw = ThinkingControlMiddleware()
    request = _make_request()

    def failing_then_recording(r):
        model = r.model
        if model.tag == "thinking=False(floor=False)":
            raise _AlwaysThinkRejectionError()
        return ("ok", model.tag)

    out = mw.wrap_model_call(request, failing_then_recording)
    assert out == ("ok", "thinking=False(floor=True)")
    assert mw._off_floor_learned is True

    # Subsequent off calls go straight to the floor variant.
    _, _, seen = _run(mw, request)
    assert all(m.tag == "thinking=False(floor=True)" for m in seen)
    assert not any(c == {"thinking": False, "level": None, "floor": False} for c in fake_build[1:])


def test_non_disable_errors_propagate(sid):
    state_register_mem.set_state(sid, _FLAG_KEY, False)
    mw = ThinkingControlMiddleware()
    request = _make_request()

    def boom(r):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        mw.wrap_model_call(request, boom)
    assert mw._off_floor_learned is False


# ----------------------------------------------------------------------
# per-session main-model override (PUT /sessions/model)
# ----------------------------------------------------------------------

_MODEL_KEY = "llm_main_model"
_PROFILE = {
    "id": "p1",
    "label": "Kimi K2",
    "provider": "openai",
    "model": "kimi-k2",
    "base_url": "https://api.moonshot.cn/v1",
    "api_key": "sk-kimi",
}


@pytest.fixture
def fake_profile_build(monkeypatch):
    """Stub build_main_llm_for_profile so no real LLM client is constructed."""
    calls: list[dict] = []

    def _fake_build(
        *,
        provider=None,
        model,
        api_key=None,
        base_url=None,
        temperature=None,
        thinking=None,
        thinking_level=None,
        thinking_floor=False,
    ):
        calls.append(
            {
                "provider": provider,
                "model": model,
                "api_key": api_key,
                "base_url": base_url,
                "temperature": temperature,
                "thinking": thinking,
                "level": thinking_level,
                "floor": thinking_floor,
            }
        )
        suffix = (
            f"level:{thinking_level}"
            if thinking_level
            else f"thinking={thinking}(floor={thinking_floor})"
        )
        return _FakeModel(f"profile:{model}|{suffix}")

    monkeypatch.setattr(models, "build_main_llm_for_profile", _fake_build)
    return calls


class TestModelOverride:
    """The session's profile descriptor swaps the model on both wrap paths."""

    def test_override_swaps_via_the_profile_builder(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()

        _, _, seen = _run(mw, _make_request())

        assert all(m.tag.startswith("profile:kimi-k2|") for m in seen)
        call = fake_profile_build[-1]
        assert call["provider"] == "openai"
        assert call["base_url"] == "https://api.moonshot.cn/v1"
        assert call["api_key"] == "sk-kimi"

    def test_override_alone_keeps_the_env_thinking_default(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()

        _run(mw, _make_request())

        call = fake_profile_build[-1]
        assert call["thinking"] is None
        assert call["level"] is None and call["floor"] is False

    def test_override_composes_with_the_thinking_choice(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        state_register_mem.set_state(sid, _FLAG_KEY, "max")
        mw = ThinkingControlMiddleware()

        _, _, seen = _run(mw, _make_request())

        assert all(m.tag == "profile:kimi-k2|level:max" for m in seen)

    def test_variant_cache_is_keyed_by_the_profile_configuration(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()
        request = _make_request()

        _run(mw, request)
        _run(mw, request)
        assert len(fake_profile_build) == 1, "one client per (thinking, profile) pair"

        # A different credential for the same model must build a NEW client.
        state_register_mem.set_state(sid, _MODEL_KEY, {**_PROFILE, "api_key": "sk-other"})
        _run(mw, request)
        assert len(fake_profile_build) == 2

    def test_malformed_override_is_ignored(self, sid, fake_profile_build):
        for junk in ("kimi-k2", {}, {"label": "no model"}, 42, [1]):
            state_register_mem.set_state(sid, _MODEL_KEY, junk)
            mw = ThinkingControlMiddleware()
            request = _make_request()
            _, _, seen = _run(mw, request)
            assert all(m is request.model for m in seen), junk
        assert fake_profile_build == []

    def test_clearing_the_override_returns_to_the_env_model(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()
        _run(mw, _make_request())

        state_register_mem.delete_state(sid, _MODEL_KEY)
        request = _make_request()
        _, _, seen = _run(mw, request)

        assert all(m is request.model for m in seen)

    def test_profile_build_failure_fails_open(self, sid, monkeypatch):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)

        def _boom(**kwargs):
            raise RuntimeError("gateway unreachable")

        monkeypatch.setattr(models, "build_main_llm_for_profile", _boom)
        mw = ThinkingControlMiddleware()
        request = _make_request()

        _, _, seen = _run(mw, request)

        assert all(m is request.model for m in seen)

    def test_disable_rejection_retry_keeps_the_override(self, sid, fake_profile_build):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        state_register_mem.set_state(sid, _FLAG_KEY, False)
        mw = ThinkingControlMiddleware()
        request = _make_request()

        def failing_then_recording(r):
            if r.model.tag == "profile:kimi-k2|thinking=False(floor=False)":
                raise _AlwaysThinkRejectionError()
            return ("ok", r.model.tag)

        out = mw.wrap_model_call(request, failing_then_recording)

        assert out == ("ok", "profile:kimi-k2|thinking=False(floor=True)")
        assert fake_profile_build[-1]["model"] == "kimi-k2"
        assert fake_profile_build[-1]["floor"] is True
