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
