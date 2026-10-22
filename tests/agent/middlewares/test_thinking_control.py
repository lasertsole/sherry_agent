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
import agent.middlewares.thinking_control.core as tc_core
from agent.middlewares.thinking_control import ThinkingControlMiddleware
from runtime import StateKey, state_register_db, state_register_mem

pytestmark = [pytest.mark.unit]

_FLAG_KEY = "llm_thinking_enabled"


class _FakeModel:
    def __init__(self, tag: str):
        self.tag = tag


@pytest.fixture(autouse=True)
def fake_build(monkeypatch):
    """Stub build_main_llm so no real LLM client is constructed."""
    calls: list[dict] = []

    def _fake_build(
        temperature=None,
        *,
        thinking=None,
        thinking_level=None,
        thinking_floor=False,
        custom_mapping=None,
    ):
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
    state_register_db.delete_state(s, StateKey.SYSTEM_PROMPT)
    tc_core._last_applied.pop(s, None)
    tc_core._applied_model_configs.pop(s, None)


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
    # Unknown tokens only: every level spelling in the seven-level enum IS a
    # valid control value now (pinned by the level tests below).
    for junk in ("true", 1, "turbo", "highest"):
        state_register_mem.set_state(sid, _FLAG_KEY, junk)
        mw = ThinkingControlMiddleware()
        request = _make_request()
        _, _, seen = _run(mw, request)
        assert all(m is request.model for m in seen), junk


def test_every_level_token_swaps_a_level_variant(sid):
    # The full seven-level enum is accepted; each token keys its own variant.
    for token in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
        state_register_mem.set_state(sid, _FLAG_KEY, token)
        mw = ThinkingControlMiddleware()
        request = _make_request()
        _, _, seen = _run(mw, request)
        assert all(m.tag == f"level:{token}" for m in seen), token


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
_PROFILE_B = {
    **_PROFILE,
    "id": "p2",
    "label": "Kimi K3",
    "model": "kimi-k3",
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
        context_window=None,
        temperature=None,
        custom_mapping=None,
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
                "context_window": context_window,
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


class TestAppliedConfigPublication:
    """The rebuildable config the memory review forks on (prefix-cache seam)."""

    def test_the_applied_config_is_published_for_the_swap(self, sid, fake_profile_build):
        from agent.middlewares.thinking_control.core import current_session_model_config

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        state_register_mem.set_state(sid, _FLAG_KEY, "max")
        mw = ThinkingControlMiddleware(temperature=0.8)

        _run(mw, _make_request())

        assert current_session_model_config(sid) == {
            "override": _PROFILE,
            "thinking_kwargs": {"thinking_level": "max"},
            "temperature": 0.8,
            "custom_mapping": None,
        }

    def test_the_env_default_is_published_when_no_control_is_set(
        self, sid, fake_build, fake_profile_build
    ):
        from agent.middlewares.thinking_control.core import current_session_model_config

        mw = ThinkingControlMiddleware(temperature=0.8)
        _run(mw, _make_request())

        assert current_session_model_config(sid) == {
            "override": None,
            "thinking_kwargs": {},
            "temperature": 0.8,
            "custom_mapping": None,
        }

    def test_clearing_the_controls_replaces_the_stale_config(self, sid, fake_profile_build):
        """A cleared control must not leave the previous variant published."""
        from agent.middlewares.thinking_control.core import current_session_model_config

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()
        _run(mw, _make_request())

        state_register_mem.delete_state(sid, _MODEL_KEY)
        _run(mw, _make_request())

        config = current_session_model_config(sid)
        assert config is not None and config["override"] is None
        assert config["thinking_kwargs"] == {}

    def test_a_build_failure_publishes_the_env_default(self, sid, monkeypatch):
        from agent.middlewares.thinking_control.core import current_session_model_config

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)

        def _boom(**kwargs):
            raise RuntimeError("gateway unreachable")

        monkeypatch.setattr(models, "build_main_llm_for_profile", _boom)
        mw = ThinkingControlMiddleware()
        _run(mw, _make_request())

        config = current_session_model_config(sid)
        assert config is not None and config["override"] is None

    def test_a_report_is_read_only(self, sid, fake_profile_build):
        from agent.middlewares.thinking_control.core import current_session_model_config

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        mw = ThinkingControlMiddleware()
        _run(mw, _make_request())

        config = current_session_model_config(sid)
        config["override"] = "mutated"

        assert current_session_model_config(sid)["override"] == _PROFILE

    def test_the_floor_retry_publishes_the_floor_config(self, sid, fake_profile_build):
        from agent.middlewares.thinking_control.core import current_session_model_config

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        state_register_mem.set_state(sid, _FLAG_KEY, False)
        mw = ThinkingControlMiddleware()
        request = _make_request()

        def failing_then_recording(r):
            if r.model.tag == "profile:kimi-k2|thinking=False(floor=False)":
                raise _AlwaysThinkRejectionError()
            return "ok"

        mw.wrap_model_call(request, failing_then_recording)

        config = current_session_model_config(sid)
        assert config is not None
        assert config["thinking_kwargs"] == {"thinking": False, "thinking_floor": True}
        assert config["override"] == _PROFILE


# ----------------------------------------------------------------------
# frozen memory snapshot refresh on a model / thinking switch
# ----------------------------------------------------------------------

_MAPPING_KEY = StateKey.CUSTOM_THINKING_MAPPING
_MAPPING_A = {
    "wire_format": "custom",
    "wire_field": "thinking.type",
    "level_map": {"off": "off", "high": "on"},
}
_MAPPING_B = {
    "wire_format": "custom",
    "wire_field": "thinking.type",
    "level_map": {"off": "off", "high": "full"},
}


def _apply_once(mw, sid):
    """Run the wrap path once, recording this turn's applied control."""
    mw.wrap_model_call(_make_request(session_id=sid), lambda _request: "ok")


def _prime_prompt(sid, value="OLD"):
    state_register_mem.set_state(sid, StateKey.SYSTEM_PROMPT, value)
    state_register_db.set_state(sid, StateKey.SYSTEM_PROMPT, value)


def _cached_prompt(sid):
    return (
        state_register_mem.get_state(sid, StateKey.SYSTEM_PROMPT, None),
        state_register_db.get_state(sid, StateKey.SYSTEM_PROMPT, None),
    )


@pytest.fixture
def refresh_calls(monkeypatch):
    """Replace the disk reload with a recorder that publishes a fresh snapshot."""
    from agent.tools.memory import memory_store

    calls: list[bool] = []

    def _load():
        calls.append(True)
        memory_store._system_prompt_snapshot = {
            "memory": "",
            "user": "",
            "facts": "FRESH",
        }

    monkeypatch.setattr(memory_store, "load_from_disk", _load)
    return calls


class TestFrozenSnapshotRefresh:
    """A switch reloads the snapshot before the first model call of the turn."""

    def test_a_model_switch_refreshes_the_snapshot(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE_B)
        # The graph (and this middleware) is rebuilt for the next turn; the
        # detection state is process-level by design.
        mw = ThinkingControlMiddleware()
        assert mw.before_model({"session_id": sid}) is None

        assert refresh_calls == [True]
        assert _cached_prompt(sid) == (None, None)

    def test_a_thinking_switch_refreshes_the_snapshot(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _FLAG_KEY, True)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        state_register_mem.set_state(sid, _FLAG_KEY, "max")
        mw = ThinkingControlMiddleware()
        assert asyncio.run(mw.abefore_model({"session_id": sid})) is None

        assert refresh_calls == [True]
        assert _cached_prompt(sid) == (None, None)

    def test_a_gateway_change_with_the_same_model_name_refreshes(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        state_register_mem.set_state(
            sid,
            _MODEL_KEY,
            {**_PROFILE, "base_url": "https://other-gateway.example/v1"},
        )
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == [True]
        assert _cached_prompt(sid) == (None, None)

    def test_a_custom_mapping_change_refreshes(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _MAPPING_KEY, _MAPPING_A)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        state_register_mem.set_state(sid, _MAPPING_KEY, _MAPPING_B)
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == [True]
        assert _cached_prompt(sid) == (None, None)

    def test_clearing_the_model_control_refreshes(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        state_register_mem.delete_state(sid, _MODEL_KEY)
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == [True]
        assert _cached_prompt(sid) == (None, None)

    def test_an_unchanged_control_keeps_the_cache(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _FLAG_KEY, True)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == []
        assert _cached_prompt(sid) == ("OLD", "OLD")

    def test_the_first_turn_keeps_the_initial_snapshot(self, sid, refresh_calls):
        _prime_prompt(sid)

        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == []
        assert _cached_prompt(sid) == ("OLD", "OLD")

    def test_the_learned_off_floor_is_not_a_second_switch(self, sid, refresh_calls):
        state_register_mem.set_state(sid, _FLAG_KEY, False)
        prior = ThinkingControlMiddleware()
        prior._off_floor_learned = True
        _apply_once(prior, sid)
        _prime_prompt(sid)

        # The next turn's fresh instance starts unlearned and computes "off";
        # the previous turn applied "off_floor". Same user-visible choice.
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert refresh_calls == []
        assert _cached_prompt(sid) == ("OLD", "OLD")

    def test_a_reload_failure_leaves_the_cached_prompt(self, sid, monkeypatch):
        from agent.tools.memory import memory_store

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE_B)

        def _boom():
            raise OSError("memory file unreadable")

        monkeypatch.setattr(memory_store, "load_from_disk", _boom)
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        assert _cached_prompt(sid) == ("OLD", "OLD")

    def test_the_first_model_call_after_a_switch_uses_the_fresh_snapshot(
        self, sid, refresh_calls, monkeypatch
    ):
        from langchain_core.messages import AIMessage, HumanMessage

        from agent.middlewares.system_prompt import core as sp_core
        from agent.middlewares.system_prompt.core import system_prompt_injection
        from agent.tools.memory import memory_store

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)
        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE_B)

        monkeypatch.setattr(
            sp_core,
            "build_system_prompt",
            lambda session_id="": memory_store.format_for_system_prompt("facts") or "",
        )
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        captured: dict[str, ModelRequest] = {}

        def handler(request):
            captured["request"] = request
            return AIMessage(content="ok")

        request = ModelRequest(
            model=_FakeModel("base"),
            messages=[HumanMessage(content="q")],
            state={"session_id": sid},
        )
        system_prompt_injection.wrap_model_call(request, handler)

        assert captured["request"].system_message.content == "FRESH"

    def test_the_refresh_reads_mid_session_writes_from_disk(self, sid, tmp_path, monkeypatch):
        from agent.tools import memory as memory_mod

        store = memory_mod.MemoryStore()
        monkeypatch.setattr(memory_mod, "MEMORY_DIR", tmp_path)
        monkeypatch.setattr(memory_mod, "memory_store", store)
        (tmp_path / "FACTS.md").write_text("old fact", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")
        (tmp_path / "USER.md").write_text("", encoding="utf-8")
        store.load_from_disk()
        assert "old fact" in (store.format_for_system_prompt("facts") or "")

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE)
        _apply_once(ThinkingControlMiddleware(), sid)
        _prime_prompt(sid)

        # A mid-session write only touches the file; the frozen snapshot must
        # still be stale until the switch-triggered reload runs.
        with (tmp_path / "FACTS.md").open("a", encoding="utf-8") as handle:
            handle.write("\n§\nnew fact")
        assert "new fact" not in (store.format_for_system_prompt("facts") or "")

        state_register_mem.set_state(sid, _MODEL_KEY, _PROFILE_B)
        assert ThinkingControlMiddleware().before_model({"session_id": sid}) is None

        refreshed = store.format_for_system_prompt("facts") or ""
        assert "old fact" in refreshed and "new fact" in refreshed
        assert _cached_prompt(sid) == (None, None)
