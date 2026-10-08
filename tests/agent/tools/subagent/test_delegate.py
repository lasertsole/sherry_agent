import asyncio

import pytest
from loguru import logger

import agent.tools.subagent.delegate as delegate
from agent.tools.subagent.delegate import DelegatedTaskHandle, delegate_task
from agent.tools.subagent.spawn.core import SpawnResult


# Deterministic skill dataset mirroring the conftest stub (see
# tests/agent/tools/subagent/conftest.py). delegate.py resolves
# skills.loader.scan_skills / get_skills_text lazily inside its call sites, so
# the autouse fixture below pins those loader attributes to this fixed dataset
# — regardless of whether the loader in sys.modules is the conftest stub
# (unit-solo runs) or the real skills.loader (full-suite collection).
_SKILL_SCOPES = {
    "web_search": "all",
    "code_interpreter": "all",
    "skill_creator": "main_only",
    "clawhub": "main_only",
}


def _scan_skills_stub(use_cache: bool = True) -> list[dict]:
    return [
        {"name": "web_search", "scope": "all"},
        {"name": "code_interpreter", "scope": "all"},
        {"name": "skill_creator", "scope": "main_only"},
        {"name": "clawhub", "scope": "main_only"},
    ]


def _get_skills_text_stub(
    selected_skill_names: list[str] | None = None,
    *,
    caller_scope: str = "main",
) -> str:
    if not selected_skill_names:
        return ""
    names = [
        n
        for n in sorted(selected_skill_names)
        if not (caller_scope == "subagent" and _SKILL_SCOPES.get(n) == "main_only")
    ]
    return "<skills>\n" + "\n".join(f'  <skill name="{n}"/>' for n in names) + "\n</skills>"


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _deterministic_skill_bindings(monkeypatch):
    """Pin the skills.loader functions delegate imports at call time to the
    deterministic dataset above — no assertion changes: the tests below keep
    asserting the same injection/drop behavior, just against a known skill set
    instead of whichever loader happened to be in sys.modules."""
    import skills.loader as skills_loader

    monkeypatch.setattr(skills_loader, "scan_skills", _scan_skills_stub)
    monkeypatch.setattr(skills_loader, "get_skills_text", _get_skills_text_stub)


def _accepted_result():
    return SpawnResult(
        status="accepted",
        child_session_key="agent:main:subagent:abc",
        run_id="run-123",
        task_name="demo_task",
        note="accepted",
    )


class TestValidation:
    def test_empty_task_raises(self):
        with pytest.raises(ValueError, match="task"):
            delegate_task("", requester_session_key="agent:main:session:x")

    def test_whitespace_task_raises(self):
        with pytest.raises(ValueError, match="task"):
            delegate_task("   ", requester_session_key="agent:main:session:x")

    def test_missing_requester_session_key_raises(self):
        # `requester_session_key` is a required keyword-only argument; omitting
        # it is a TypeError, not a runtime ValueErrror.
        with pytest.raises(TypeError):
            delegate_task("do something")

    def test_empty_requester_session_key_raises(self):
        with pytest.raises(ValueError, match="requester_session_key"):
            delegate_task("do something", requester_session_key="")

    def test_max_spawn_depth_cap_rejected(self):
        with pytest.raises(ValueError, match="cannot exceed") as excinfo:
            delegate_task(
                "do something", requester_session_key="agent:main:session:x", max_spawn_depth=3
            )
        # Plain ValueError from the delegate-level hard cap — NOT pydantic's
        # validate_assignment ValidationError (a ValueError subclass whose
        # message also contains "cannot exceed"); asserting the exact type is
        # what makes the mutation-QA (deleting the cap check) turn red.
        assert type(excinfo.value) is ValueError

    def test_context_mode_kwarg_rejected(self):
        with pytest.raises(TypeError, match="context_mode"):
            delegate_task(
                "do something",
                requester_session_key="agent:main:session:x",
                context_mode="isolated",
            )


class TestSkillInjection:
    def test_unknown_skills_dropped(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["task"] = kwargs["task"]
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        delegate_task(
            "base task",
            requester_session_key="agent:main:session:x",
            load_skills=["web_search", "does_not_exist"],
            run_in_background=True,
        )
        # Unknown skill ignored; existing skill injected as XML block.
        assert "web_search" in seen["task"]
        assert "does_not_exist" not in seen["task"]

    def test_auth_skills_excluded(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["task"] = kwargs["task"]
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        delegate_task(
            "base task",
            requester_session_key="agent:main:session:x",
            load_skills=["clawhub", "skill_creator", "web_search"],
            run_in_background=True,
        )
        # main_only-scoped skills (clawhub/skill_creator) are silently
        # excluded via their `scope:` frontmatter; web_search still injected.
        assert "clawhub" not in seen["task"]
        assert "skill_creator" not in seen["task"]
        assert "web_search" in seen["task"]


class TestValidateLoadSkills:
    """Direct coverage for the scope-based drop in _validate_load_skills."""

    def test_main_only_scope_dropped(self):
        # skill_creator/clawhub are `scope: main_only` in the skills.loader
        # stub; a subagent caller must not resolve them.
        resolved = delegate._validate_load_skills(["skill_creator", "web_search"])
        assert resolved == ["web_search"]

    def test_all_main_only_resolves_empty(self):
        assert delegate._validate_load_skills(["clawhub", "skill_creator"]) == []

    def test_unknown_names_dropped_and_warned(self):
        # delegate logs through loguru, which stdlib caplog does not see.
        messages: list[str] = []
        handler_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            resolved = delegate._validate_load_skills(["nope", "web_search"])
        finally:
            logger.remove(handler_id)

        assert resolved == ["web_search"]
        assert any("unknown skill" in message for message in messages), messages

    def test_empty_and_none(self):
        assert delegate._validate_load_skills(None) == []
        assert delegate._validate_load_skills([]) == []

    def test_no_skills_leaves_task_untouched(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["task"] = kwargs["task"]
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        delegate_task(
            "plain task",
            requester_session_key="agent:main:session:x",
            load_skills=[],
            run_in_background=True,
        )
        assert seen["task"] == "plain task"


class TestDispatchModes:
    def test_background_returns_accepted_handle(self, monkeypatch):
        async def _fake(*args, **kwargs):
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        h = delegate_task(
            "do something",
            requester_session_key="agent:main:session:x",
            run_in_background=True,
        )
        assert isinstance(h, DelegatedTaskHandle)
        assert h.accepted
        assert h.run_id == "run-123"
        assert h.child_session_key == "agent:main:subagent:abc"
        assert h.background is True

    def test_blocking_spawn_forwards_default_cleanup(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["cleanup"] = kwargs.get("cleanup")
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        # Blocking mode spawns; since our fake returns immediate accepted and
        # _await_outside_loop polls is_running() which is False (no run_id in
        # registry), the handle returns promptly.
        h = delegate_task(
            "do something",
            requester_session_key="agent:main:session:x",
            run_in_background=False,
        )
        assert h.accepted
        assert seen["cleanup"] == "delete"

    def test_per_call_overrides_restored(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["run_timeout_seconds"] = kwargs.get("run_timeout_seconds")
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        from agent.tools.subagent.config import get_config

        cfg = get_config()
        orig = cfg.run_timeout_seconds
        delegate_task(
            "do something",
            requester_session_key="agent:main:session:x",
            run_timeout_seconds=42.0,
            run_in_background=True,
        )
        assert seen["run_timeout_seconds"] == 42.0
        # Global config restored after dispatch.
        assert cfg.run_timeout_seconds == orig

    def test_max_concurrent_override_restored(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["max_concurrent"] = get_config().max_concurrent
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        from agent.tools.subagent.config import get_config

        orig = get_config().max_concurrent
        with pytest.warns(DeprecationWarning, match="max_concurrent"):
            delegate_task(
                "do something",
                requester_session_key="agent:main:session:x",
                max_concurrent=3,
                run_in_background=True,
            )
        assert seen["max_concurrent"] == 3
        # Global singleton restored after dispatch.
        assert get_config().max_concurrent == orig


class TestHandleHelpers:
    def test_to_dict(self):
        h = DelegatedTaskHandle(
            status="accepted",
            child_session_key="k",
            run_id="r",
            task_name="t",
            background=True,
        )
        d = h.to_dict()
        assert d["status"] == "accepted"
        assert d["run_id"] == "r"
        assert d["background"] is True

    def test_is_running_false_when_not_accepted(self):
        h = DelegatedTaskHandle(status="forbidden", error="nope")
        assert h.accepted is False
        assert h.forbidden is True
        assert h.is_running() is False

    def test_terminal_text_reads_nested_record_fields(self, monkeypatch):
        """result_text/error live on nested completion/execution.outcome models,
        NOT at the top level of SubagentRunRecord (regression for field access)."""
        from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

        class _FakeRun:
            class _Execution:
                outcome = RunOutcome(status=RunOutcomeStatus.OK, error=None)

            class _Completion:
                result_text = "mock result"

            execution = _Execution()
            completion = _Completion()

        monkeypatch.setattr(delegate, "get_run", lambda run_id: _FakeRun())
        result_text, err = delegate._terminal_text("run-x")
        assert result_text == "mock result"
        assert err is None

    def test_terminal_text_reports_outcome_error(self, monkeypatch):
        from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

        class _FakeRun:
            class _Execution:
                outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error="boom")

            class _Completion:
                result_text = None

            execution = _Execution()
            completion = _Completion()

        monkeypatch.setattr(delegate, "get_run", lambda run_id: _FakeRun())
        result_text, err = delegate._terminal_text("run-x")
        assert result_text is None
        assert err == "boom"


class TestLoopGuard:
    """delegate_task owns its own event loop (asyncio.run / run_until_complete on
    a fresh loop), so calling it from a running-loop thread must fail loudly and
    name the async alternative instead of surfacing a cryptic asyncio error."""

    def test_called_from_running_loop_raises_with_alternative(self):
        async def _call():
            delegate_task(
                "do something",
                requester_session_key="agent:main:session:x",
                run_in_background=True,
            )

        with pytest.raises(RuntimeError, match="spawn_subagent_direct|to_thread"):
            asyncio.run(_call())

    def test_guard_fires_before_validation(self):
        # The loop check precedes argument validation: an invalid task from a
        # loop thread still reports the loop violation, not the task error.
        async def _call():
            delegate_task("", requester_session_key="")

        with pytest.raises(RuntimeError, match="running event loop"):
            asyncio.run(_call())


class TestResultPolling:
    """``result()`` is synchronous-only; :meth:`result_async` is the loop-safe twin."""

    @staticmethod
    def _terminal_run():
        from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

        class _FakeRun:
            class _Execution:
                # ``poll()`` only lifts the terminal fields once the execution
                # status reads TERMINAL.
                status = "TERMINAL"
                outcome = RunOutcome(status=RunOutcomeStatus.OK, error=None)

            class _Completion:
                result_text = "mock result"

            execution = _Execution()
            completion = _Completion()

        return _FakeRun()

    def test_result_from_a_loop_thread_raises_with_the_async_twin(self, monkeypatch):
        handle = DelegatedTaskHandle(status="accepted", run_id="r1")
        monkeypatch.setattr(delegate, "get_run", lambda run_id: self._terminal_run())

        async def _call():
            handle.result()

        with pytest.raises(RuntimeError, match="result_async"):
            asyncio.run(_call())

    def test_result_outside_a_loop_still_blocks_and_populates(self, monkeypatch):
        handle = DelegatedTaskHandle(status="accepted", run_id="r1")
        handle.is_running = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr(delegate, "get_run", lambda run_id: self._terminal_run())

        out = handle.result(poll_interval=0)

        assert out is handle
        assert out.result_text == "mock result"

    @pytest.mark.asyncio
    async def test_result_async_polls_without_blocking_the_loop(self, monkeypatch):
        handle = DelegatedTaskHandle(status="accepted", run_id="r1")
        state = {"done": False, "ticks": 0}
        handle.is_running = lambda: not state["done"]  # type: ignore[method-assign]
        monkeypatch.setattr(delegate, "get_run", lambda run_id: self._terminal_run())

        async def _ticker():
            while not state["done"]:
                state["ticks"] += 1
                await asyncio.sleep(0.001)

        async def _finish_soon():
            await asyncio.sleep(0.03)
            state["done"] = True

        ticker = asyncio.create_task(_ticker())
        result, _ = await asyncio.gather(handle.result_async(poll_interval=0.005), _finish_soon())
        await ticker

        assert result is handle
        assert result.result_text == "mock result"
        assert state["ticks"] > 1, "the loop kept running while result_async waited"
