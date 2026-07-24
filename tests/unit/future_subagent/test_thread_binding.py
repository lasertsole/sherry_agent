import pytest
from future_subagent.spawn.thread_binding import (
    ThreadBindingConfig,
    ThreadBindingInfo,
    ThreadBindingResult,
    bind_thread_for_subagent_spawn,
    unbind_thread_on_cleanup,
    refresh_thread_binding,
    resolve_thread_binding_policy,
)
from future_subagent.types.spawn import SpawnMode


class TestThreadBindingConfig:
    def test_defaults(self):
        cfg = ThreadBindingConfig()
        assert cfg.idle_timeout_ms == 300000
        assert cfg.max_age_ms == 86400000
        assert cfg.thread_name is None
        assert cfg.intro_text is None

    def test_custom(self):
        cfg = ThreadBindingConfig(
            idle_timeout_ms=60000,
            max_age_ms=3600000,
            thread_name="worker-1",
            intro_text="Hello",
        )
        assert cfg.idle_timeout_ms == 60000
        assert cfg.max_age_ms == 3600000
        assert cfg.thread_name == "worker-1"
        assert cfg.intro_text == "Hello"


class TestThreadBindingInfo:
    def test_construction(self):
        info = ThreadBindingInfo(
            thread_id="thread:subagent:abc",
            bound_at=100.0,
            idle_timeout_ms=60000,
            delivery_origin="agent:main:subagent:child",
        )
        assert info.thread_id == "thread:subagent:abc"
        assert info.bound_at == 100.0
        assert info.delivery_origin == "agent:main:subagent:child"

    def test_defaults(self):
        info = ThreadBindingInfo(thread_id="t1")
        assert info.idle_timeout_ms == 300000
        assert info.max_age_ms == 86400000
        assert info.delivery_origin is None


class TestBindThreadForSubagentSpawn:
    def test_bind_success(self):
        result = bind_thread_for_subagent_spawn("agent:main:subagent:child1")
        assert result.bound is True
        assert result.thread_id is not None
        assert result.binding_info is not None
        assert result.delivery_origin is not None

    def test_bind_with_config(self):
        cfg = ThreadBindingConfig(idle_timeout_ms=60000, thread_name="test-thread")
        result = bind_thread_for_subagent_spawn("agent:main:subagent:child1", config=cfg)
        assert result.bound is True
        assert result.binding_info.idle_timeout_ms == 60000

    def test_bind_none_session_key(self):
        result = bind_thread_for_subagent_spawn(None)
        assert result.bound is False

    def test_binding_info_has_delivery_origin(self):
        result = bind_thread_for_subagent_spawn("agent:main:subagent:child1")
        assert result.binding_info.delivery_origin == "agent:main:subagent:child1"

    def test_thread_id_format(self):
        result = bind_thread_for_subagent_spawn("agent:main:subagent:child1")
        assert result.thread_id.startswith("thread:subagent:")


class TestUnbindThreadOnCleanup:
    def test_unbind_with_id(self):
        unbind_thread_on_cleanup("thread:subagent:abc")

    def test_unbind_none(self):
        unbind_thread_on_cleanup(None)


class TestRefreshThreadBinding:
    def test_refresh_with_id(self):
        refresh_thread_binding("thread:subagent:abc")

    def test_refresh_none(self):
        refresh_thread_binding(None)


class TestResolveThreadBindingPolicy:
    def test_session_mode_binds(self):
        result = resolve_thread_binding_policy(
            agent_id="main",
            spawn_mode=SpawnMode.SESSION,
            child_session_key="agent:main:subagent:child1",
        )
        assert result.bound is True
        assert result.thread_id is not None

    def test_run_mode_no_bind(self):
        result = resolve_thread_binding_policy(
            agent_id="main",
            spawn_mode=SpawnMode.RUN,
            child_session_key="agent:main:subagent:child1",
        )
        assert result.bound is False
        assert result.thread_id is None

    def test_session_mode_with_config(self):
        cfg = ThreadBindingConfig(idle_timeout_ms=120000)
        result = resolve_thread_binding_policy(
            agent_id="main",
            spawn_mode=SpawnMode.SESSION,
            child_session_key="agent:main:subagent:child1",
            config=cfg,
        )
        assert result.bound is True
        assert result.binding_info.idle_timeout_ms == 120000
