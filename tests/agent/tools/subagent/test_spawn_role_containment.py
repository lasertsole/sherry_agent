"""Capability containment: a specialized parent may only spawn safe roles.

The hole this closes: a child's tools come from the FULL main tool set filtered
by the CHILD's own functional role, so before containment a read-only researcher
at ORCHESTRATOR depth could spawn a write-capable executor and have the writing
done on its behalf. Same-role derivation (same role = same whitelist) removes the
escalation path without producing "crippled" children the way a tool
intersection would.

The rule is now table-driven (``SPAWN_ALLOWED_CHILD_ROLES``): an ABSENT entry
still means same-role only, and an EXECUTOR entry adds the read-only researcher
and librarian roles so an executor can research before it writes. Containment
guards WRITE capability, so permitting read-only extras cannot escalate — the
invariant tests at the bottom hold that line open for future entries.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.tools.subagent.spawn.core import _check_role_containment, spawn_subagent_direct
from agent.tools.subagent.types.functional_role import FunctionalRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_CALLER = "agent:main:subagent:parent-1"


class _ParentRun:
    depth = 1
    spawned_by = "agent:main:session:owner"
    requester_session_key = "agent:main:session:owner"

    def __init__(self, role: FunctionalRole):
        self.functional_role = role


@pytest.fixture()
def parent_run(monkeypatch):
    """Resolve the requesting session's own run record from a stub."""
    import agent.tools.subagent.registry as registry

    holder: dict[str, _ParentRun | None] = {"run": None}
    monkeypatch.setattr(registry, "get_run_by_child_session_key", lambda key: holder["run"])
    return holder


class TestContainmentMatrix:
    def test_main_is_unrestricted(self, parent_run):
        # No run record for the requester = a MAIN session, the orchestration root.
        parent_run["run"] = None

        assert _check_role_containment(_CALLER, FunctionalRole.EXECUTOR) is None
        assert _check_role_containment(_CALLER, FunctionalRole.RESEARCHER) is None

    def test_general_is_unrestricted(self, parent_run):
        parent_run["run"] = _ParentRun(FunctionalRole.GENERAL)

        assert _check_role_containment(_CALLER, FunctionalRole.EXECUTOR) is None
        assert _check_role_containment(_CALLER, FunctionalRole.GENERAL) is None

    @pytest.mark.parametrize("role", list(FunctionalRole))
    def test_a_specialized_parent_may_spawn_its_own_role(self, parent_run, role):
        if role == FunctionalRole.GENERAL:
            pytest.skip("general is the unrestricted case above")
        parent_run["run"] = _ParentRun(role)

        assert _check_role_containment(_CALLER, role) is None

    @pytest.mark.parametrize(
        ("parent", "child"),
        [
            # A read-only parent can never reach a write role (the original hole).
            (FunctionalRole.RESEARCHER, FunctionalRole.EXECUTOR),
            (FunctionalRole.RESEARCHER, FunctionalRole.GENERAL),
            (FunctionalRole.RESEARCHER, FunctionalRole.REVIEWER),
            (FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN),
            # An executor's widening covers READ-ONLY roles only: general is an
            # escalation (every tool) and reviewer is semantically off-limits.
            (FunctionalRole.EXECUTOR, FunctionalRole.GENERAL),
            (FunctionalRole.EXECUTOR, FunctionalRole.REVIEWER),
            (FunctionalRole.REVIEWER, FunctionalRole.EXECUTOR),
            (FunctionalRole.LIBRARIAN, FunctionalRole.EXECUTOR),
        ],
    )
    def test_cross_role_spawning_is_refused(self, parent_run, parent, child):
        parent_run["run"] = _ParentRun(parent)

        refusal = _check_role_containment(_CALLER, child)

        assert refusal is not None
        assert f"a {parent} subagent can only spawn" in refusal
        assert f"(requested {child})" in refusal

    @pytest.mark.parametrize("child", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
    def test_executor_may_spawn_read_only_research_children(self, parent_run, child):
        """An executor researches before it writes: read-only roles are allowed."""
        parent_run["run"] = _ParentRun(FunctionalRole.EXECUTOR)

        assert _check_role_containment(_CALLER, child) is None

    def test_the_refusal_names_the_way_out(self, parent_run):
        """The message must be actionable: it names every allowed role."""
        parent_run["run"] = _ParentRun(FunctionalRole.RESEARCHER)

        refusal = _check_role_containment(_CALLER, FunctionalRole.EXECUTOR)

        assert refusal is not None
        assert "a researcher subagent can only spawn" in refusal
        assert "researcher" in refusal  # the allowed set is named
        assert "functional_role=" in refusal
        assert "use a general subagent or the main agent" in refusal

    def test_an_executor_refusal_names_the_widened_set(self, parent_run):
        """The widened parent's refusal must advertise the read-only extras too."""
        parent_run["run"] = _ParentRun(FunctionalRole.EXECUTOR)

        refusal = _check_role_containment(_CALLER, FunctionalRole.GENERAL)

        assert refusal is not None
        assert "these roles: executor / librarian / researcher" in refusal


class TestContainmentInTheSpawnPipeline:
    """The refusal happens in the pipeline, not only in the helper."""

    def test_a_specialized_parent_gets_a_forbidden_result(self, parent_run):
        parent_run["run"] = _ParentRun(FunctionalRole.RESEARCHER)

        result = asyncio.run(
            spawn_subagent_direct(
                task="do the thing",
                requester_session_key=_CALLER,
                functional_role_hint="executor",
            )
        )

        assert result.status == "forbidden"
        assert "Capability containment" in (result.error or "")

    def test_a_specialized_parent_spawning_without_a_hint_is_refused(self, parent_run):
        """No hint resolves to GENERAL — which would be an escalation for a
        specialized parent, so it is refused rather than silently re-roled."""
        parent_run["run"] = _ParentRun(FunctionalRole.LIBRARIAN)

        result = asyncio.run(
            spawn_subagent_direct(task="do the thing", requester_session_key=_CALLER)
        )

        assert result.status == "forbidden"
        assert "(requested general)" in (result.error or "")

    def test_a_same_role_spawn_passes_the_gate(self, parent_run, monkeypatch):
        """Containment lets a same-role spawn through to the lane."""
        parent_run["run"] = _ParentRun(FunctionalRole.RESEARCHER)

        import agent.tools.subagent.spawn.core as core
        from agent.tools.subagent.registry import clear as clear_registry

        reached: list[dict] = []

        async def _record_lane(**kwargs) -> None:
            reached.append(kwargs)

        monkeypatch.setattr(core, "_execute_subagent_with_lane", _record_lane)

        async def _drive():
            result = await spawn_subagent_direct(
                task="do the thing",
                requester_session_key=_CALLER,
                functional_role_hint="researcher",
            )
            # The lane dispatch is fire-and-forget: give the task a tick to start
            # before the loop closes, or it is cancelled before its first await.
            await asyncio.sleep(0.05)
            return result

        try:
            result = asyncio.run(_drive())
        finally:
            clear_registry()

        assert result.status == "accepted", result.error
        assert reached, "the same-role spawn must reach the lane"

    def test_an_executor_reaches_the_lane_for_a_researcher_child(self, parent_run, monkeypatch):
        """The widening applies in the PIPELINE, not only in the helper."""
        parent_run["run"] = _ParentRun(FunctionalRole.EXECUTOR)

        import agent.tools.subagent.spawn.core as core
        from agent.tools.subagent.registry import clear as clear_registry

        reached: list[dict] = []

        async def _record_lane(**kwargs) -> None:
            reached.append(kwargs)

        monkeypatch.setattr(core, "_execute_subagent_with_lane", _record_lane)

        async def _drive():
            result = await spawn_subagent_direct(
                task="research the thing",
                requester_session_key=_CALLER,
                functional_role_hint="researcher",
            )
            await asyncio.sleep(0.05)
            return result

        try:
            result = asyncio.run(_drive())
        finally:
            clear_registry()

        assert result.status == "accepted", result.error
        assert reached, "the read-only research child must reach the lane"


class TestRuntimeToolExposesTheRole:
    """Same-role fan-out must be expressible through the model's spawn tool.

    The model-facing tool did not carry ``functional_role`` at all — without it
    a specialized parent could never NAME its own role, so containment would
    turn every one of its spawns into a refusal.
    """

    def test_the_schema_lists_the_roles(self):
        from agent.tools.subagent.tools import runtime_tools

        props = runtime_tools.sessions_spawn_runtime_tool.args_schema.model_json_schema()[
            "properties"
        ]

        assert "functional_role" in props
        description = props["functional_role"]["description"]
        for role in ("general", "researcher", "executor", "reviewer", "librarian"):
            assert role in description
        assert "own role" in description
        assert "executor may also spawn researcher" in description

    @pytest.mark.asyncio
    async def test_the_tool_forwards_the_role_to_the_pipeline(self, monkeypatch):
        from agent.tools.subagent.spawn.core import SpawnResult
        from agent.tools.subagent.tools import runtime_tools

        seen: dict = {}

        async def _fake(**kwargs) -> SpawnResult:
            seen.update(kwargs)
            return SpawnResult(
                status="accepted", run_id="r1", child_session_key="agent:main:subagent:x"
            )

        import agent.tools.subagent.spawn as spawn_pkg

        monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake)
        import agent.tools.subagent.spawn.privilege as privilege

        monkeypatch.setattr(privilege, "check_spawn_permission", lambda session_id: (True, ""))

        await runtime_tools.sessions_spawn_runtime_tool.coroutine(
            task="do it", session_id="s1", functional_role="researcher"
        )

        assert seen["functional_role_hint"] == "researcher"


class TestSpawnAllowedMapInvariants:
    """The map's two safety invariants, pinned so a future edit cannot drift.

    Multi-hop safety rests on ONE single-hop rule: an entry may only permit a
    read-only role besides the parent itself. Then a write role reaches only
    write roles and a read-only role reaches only its own kind — no path ever
    returns to a write capability, so per-hop checking equals ancestor-chain
    checking and no cycle detection is needed.
    """

    @staticmethod
    def _write_capable_roles() -> set[FunctionalRole]:
        """Roles with any write/execute capability (from their AGENTS.md tool list)."""
        from agent.tools.subagent.roles.loader import load_role_definition

        roles = set()
        for role in FunctionalRole:
            if role is FunctionalRole.GENERAL:
                continue  # inherit mode: every tool, unrestricted by design
            definition = load_role_definition(role)
            tools = set(definition.tools) if definition is not None else set()
            if {"write_file", "patch_file", "python_repl"} & tools:
                roles.add(role)
        return roles

    def test_every_entry_keeps_same_role_fan_out(self):
        from agent.tools.subagent.types.functional_role import SPAWN_ALLOWED_CHILD_ROLES

        assert SPAWN_ALLOWED_CHILD_ROLES, "an empty map is fine but this one is not empty"
        for parent, allowed in SPAWN_ALLOWED_CHILD_ROLES.items():
            assert parent in allowed, f"{parent} must keep its own role allowed"

    def test_no_entry_permits_a_write_capable_child(self):
        from agent.tools.subagent.types.functional_role import SPAWN_ALLOWED_CHILD_ROLES

        write_roles = self._write_capable_roles()
        assert FunctionalRole.EXECUTOR in write_roles, "the probe must find the write role"
        for parent, allowed in SPAWN_ALLOWED_CHILD_ROLES.items():
            for child in allowed:
                assert child == parent or child not in write_roles, (
                    f"{parent} must not be able to spawn the write-capable {child}"
                )

    def test_unlisted_roles_fall_back_to_same_role_only(self):
        from agent.tools.subagent.types.functional_role import SPAWN_ALLOWED_CHILD_ROLES

        for role in FunctionalRole:
            if role is FunctionalRole.GENERAL:
                continue  # unrestricted by design; never reads the map
            if role not in SPAWN_ALLOWED_CHILD_ROLES:
                assert role not in SPAWN_ALLOWED_CHILD_ROLES.get(role, frozenset())
        # The one widened role is executor; the read-only three stay closed.
        assert set(SPAWN_ALLOWED_CHILD_ROLES) == {FunctionalRole.EXECUTOR}
