"""The sessions_spawn wall-clock timeout: model-facing field, clamp, pass-through.

The model-provided value is the only UNTRUSTED input: the tool clamps it into
``[run_timeout_min_seconds, run_timeout_max_seconds]`` (600 s .. 7200 s) and
``None``/non-positive means "unset" (the resolver falls back to the global
default). Internal callers — TaskFlow dispatch, ``delegate_task`` — bypass the
clamp on purpose and keep their own bounds.
"""

import time

import pytest

from agent.tools.subagent.config import SubagentConfig, get_config, set_config
from agent.tools.subagent.spawn.plan import clamp_run_timeout_seconds, resolve_run_timeout_seconds
from agent.tools.subagent.spawn.core import SpawnResult
from agent.tools.subagent.tools import runtime_tools
from agent.tools.subagent.tools.sessions_spawn import SessionsSpawnTool

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def captured_spawn(monkeypatch):
    """Capture the kwargs either surface hands to spawn_subagent_direct.

    The runtime tool imports the function inside its body (package attribute at
    call time); the build-time module binds it at import. Both are patched.
    """
    seen: dict = {}

    async def _fake(**kwargs) -> SpawnResult:
        seen.update(kwargs)
        return SpawnResult(
            status="accepted", run_id="run-1", child_session_key="agent:main:subagent:x"
        )

    import agent.tools.subagent.spawn as spawn_pkg
    import agent.tools.subagent.tools.sessions_spawn as build_time_module

    monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake)
    monkeypatch.setattr(build_time_module, "spawn_subagent_direct", _fake)
    return seen


class TestClamp:
    def test_none_and_non_positive_read_as_unset(self):
        assert clamp_run_timeout_seconds(None) is None
        assert clamp_run_timeout_seconds(0) is None
        assert clamp_run_timeout_seconds(-1) is None

    def test_below_the_floor_clamps_up(self):
        assert clamp_run_timeout_seconds(60) == 600.0

    def test_above_the_ceiling_clamps_down(self):
        assert clamp_run_timeout_seconds(99999) == 7200.0

    def test_bounds_and_interior_values_pass_through(self):
        assert clamp_run_timeout_seconds(600) == 600.0
        assert clamp_run_timeout_seconds(7200) == 7200.0
        assert clamp_run_timeout_seconds(1800) == 1800.0
        assert clamp_run_timeout_seconds(900.5) == 900.5

    def test_the_bounds_are_configurable(self):
        original = get_config()
        set_config(original.model_copy(update={"run_timeout_min_seconds": 30.0}))
        try:
            assert clamp_run_timeout_seconds(45) == 45.0
        finally:
            set_config(original)

    def test_defaults_and_resolver_agree(self):
        config = SubagentConfig()
        assert config.run_timeout_min_seconds == 600.0
        assert config.run_timeout_max_seconds == 7200.0
        # Unset stays unset for the shared resolver — which then reads the global
        # default. The clamp never rewrites it to a number of its own.
        assert resolve_run_timeout_seconds(clamp_run_timeout_seconds(None)) == 600.0


class TestRuntimeToolSurface:
    def test_the_schema_exposes_the_field_with_its_bounds(self):
        props = runtime_tools.sessions_spawn_runtime_tool.args_schema.model_json_schema()[
            "properties"
        ]

        assert "run_timeout_seconds" in props
        description = props["run_timeout_seconds"]["description"]
        assert "600" in description and "7200" in description

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("passed", "expected"),
        [(1800, 1800.0), (60, 600.0), (99999, 7200.0), (None, None)],
    )
    async def test_the_tool_passes_the_clamped_value_through(
        self, captured_spawn, monkeypatch, passed, expected
    ):
        # The tool imports the gate inside its body, so the privilege module's
        # attribute is what a call looks up.
        import agent.tools.subagent.spawn.privilege as privilege

        monkeypatch.setattr(privilege, "check_spawn_permission", lambda session_id: (True, ""))

        await runtime_tools.sessions_spawn_runtime_tool.coroutine(
            task="do it", session_id="s1", run_timeout_seconds=passed
        )

        assert captured_spawn["run_timeout_seconds"] == expected


class TestBuildTimeToolSurface:
    @pytest.mark.asyncio
    async def test_the_class_tool_clamps_before_spawning(self, captured_spawn):
        await SessionsSpawnTool(session_id="s1")._arun(task="do it", run_timeout_seconds=60)

        assert captured_spawn["run_timeout_seconds"] == 600.0

    @pytest.mark.asyncio
    async def test_unset_reaches_the_internal_api_as_none(self, captured_spawn):
        await SessionsSpawnTool(session_id="s1")._arun(task="do it")

        assert captured_spawn["run_timeout_seconds"] is None


class TestTheRunBudgetIsOneDeadline:
    """P3-12: ``run_timeout_seconds`` bounds the RUN, not each model call.

    The goal loop can make ``goal_max_turns`` calls through the same ``_invoke``;
    a per-call timeout multiplied the advertised bound by the turn budget.
    """

    def test_the_remaining_budget_shrinks_across_calls(self):
        from agent.tools.subagent.spawn.core import _RunWallClock

        budget = _RunWallClock(1.0)
        first = budget.remaining()
        time.sleep(0.05)
        second = budget.remaining()

        assert first is not None and second is not None
        assert 0 < second < first <= 1.0

    def test_a_disabled_budget_never_bounds(self):
        from agent.tools.subagent.spawn.core import _RunWallClock

        assert _RunWallClock(0).remaining() is None
        assert _RunWallClock(-5).remaining() is None

    def test_an_exhausted_budget_raises_timeout(self):
        from agent.tools.subagent.spawn.core import _RunWallClock

        budget = _RunWallClock(0.01)
        time.sleep(0.02)

        with pytest.raises(TimeoutError):
            budget.remaining()
