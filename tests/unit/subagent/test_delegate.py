import pytest

import agent.tools.subagent.delegate as delegate
from agent.tools.subagent.delegate import DelegatedTaskHandle, delegate_task
from agent.tools.subagent.spawn.core import SpawnResult
from agent.tools.subagent.types.spawn import ContextMode


# Deterministic skill dataset mirroring the conftest stub (see
# tests/unit/subagent/conftest.py). delegate.py:45 binds get_skills_text /
# scan_skills at module import time, so WHICH module object those names come
# from depends on when agent.tools.subagent was first imported (conftest stub
# in unit-solo runs, real skills.loader in full-suite collection). The
# autouse fixture below pins the module-level bindings to this fixed dataset
# so the skill-injection expectations hold regardless of binding order.
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


@pytest.fixture(autouse=True)
def _deterministic_skill_bindings(monkeypatch):
    """Pin delegate's module-level skill bindings (delegate.py:45) to the
    deterministic dataset above — binding-order robustness, no assertion
    changes: the tests below keep asserting the same injection/drop
    behavior, just against a known skill set instead of whichever module
    happened to be in sys.modules at import time."""
    monkeypatch.setattr(delegate, "scan_skills", _scan_skills_stub)
    monkeypatch.setattr(delegate, "get_skills_text", _get_skills_text_stub)


def _accepted_result():
    return SpawnResult(
        status="accepted",
        child_session_key="agent:main:subagent:abc",
        run_id="run-123",
        task_name="demo_task",
        mode=None,
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

    def test_fork_context_mode_raises(self):
        with pytest.raises(ValueError, match="ISOLATED"):
            delegate_task(
                "do something",
                requester_session_key="agent:main:session:x",
                context_mode="fork",
            )

    def test_unknown_context_mode_string_raises(self):
        with pytest.raises(ValueError, match="unknown context_mode"):
            delegate_task(
                "do something",
                requester_session_key="agent:main:session:x",
                context_mode="nonsense",
            )

    def test_context_mode_accepts_enum_isolated(self, monkeypatch):
        async def _fake(*args, **kwargs):
            return _accepted_result()

        monkeypatch.setattr(delegate, "spawn_subagent_direct", _fake)
        h = delegate_task(
            "do something",
            requester_session_key="agent:main:session:x",
            context_mode=ContextMode.ISOLATED,
            run_in_background=True,
        )
        assert h.status == "accepted"


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

    def test_unknown_names_dropped_and_warned(self, caplog):
        with caplog.at_level("WARNING", logger="agent.tools.subagent.delegate"):
            resolved = delegate._validate_load_skills(["nope", "web_search"])
        assert resolved == ["web_search"]
        assert "unknown skill" in caplog.text

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

    def test_blocking_spawn_uses_run_mode(self, monkeypatch):
        seen = {}

        async def _fake(*args, **kwargs):
            seen["spawn_mode"] = kwargs.get("spawn_mode")
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
        assert seen["spawn_mode"].value == "run"
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
