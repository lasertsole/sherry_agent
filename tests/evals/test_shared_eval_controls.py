"""C-class eval consistency controls: shared temperature/budget/session/checklist.

These are evals-internal correctness fixes; the product side is untouched.
Every helper must be importable WITHOUT a model key, Docker or the server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.shared.eval_checklist import acceptance_checklist
from evals.shared.eval_iteration_budget import (
    eval_main_max_iterations,
    pin_eval_budget,
    resolve_eval_iteration_budget,
    restore_eval_budget,
)

pytestmark = [pytest.mark.unit]


# ── temperature ─────────────────────────────────────────────────────────────


def test_pin_eval_temperature_pins_zero_and_is_idempotent(monkeypatch):
    import server.service.messages as messages

    calls: list[dict] = []

    async def _fake_built_agent(*args, **kwargs):
        calls.append(kwargs)
        return "graph"

    monkeypatch.setattr(messages, "built_agent", _fake_built_agent, raising=False)
    # The module marker may be stale from a previous test; clear it by resetting
    # the seam to a bare function before pinning.
    from evals.shared import eval_temperature

    assert not getattr(messages.built_agent, eval_temperature._PIN_MARKER, False)
    eval_temperature.pin_eval_temperature()
    pinned = messages.built_agent
    eval_temperature.pin_eval_temperature()  # second pin must not stack
    assert messages.built_agent is pinned

    import asyncio

    assert asyncio.run(pinned()) == "graph"
    assert calls == [{"temperature": 0.0}]


def test_pin_eval_temperature_accepts_an_explicit_value(monkeypatch):
    import asyncio

    import server.service.messages as messages

    from evals.shared import eval_temperature

    seen: list[float] = []

    async def _fake(*args, **kwargs):
        seen.append(kwargs["temperature"])
        return None

    monkeypatch.setattr(messages, "built_agent", _fake, raising=False)
    eval_temperature.pin_eval_temperature(0.25)
    asyncio.run(messages.built_agent())
    assert seen == [0.25]


# ── iteration budget ────────────────────────────────────────────────────────


def test_pin_eval_budget_zero_reads_current_without_changing(monkeypatch):
    from config.features import ITERATION_BUDGET

    restore_eval_budget()
    monkeypatch.setitem(ITERATION_BUDGET, "main_agent_max_iterations", 180)
    assert pin_eval_budget(0) == 180
    assert ITERATION_BUDGET["main_agent_max_iterations"] == 180


def test_pin_eval_budget_positive_then_restore(monkeypatch):
    from config.features import ITERATION_BUDGET

    restore_eval_budget()
    monkeypatch.setitem(ITERATION_BUDGET, "main_agent_max_iterations", 180)
    assert pin_eval_budget(300) == 300
    assert eval_main_max_iterations() == 300
    restore_eval_budget()
    assert ITERATION_BUDGET["main_agent_max_iterations"] == 180
    restore_eval_budget()  # idempotent
    assert ITERATION_BUDGET["main_agent_max_iterations"] == 180


def test_pin_eval_budget_captures_original_before_first_pin_only(monkeypatch):
    """A second pin in the same interpreter must not overwrite the original."""
    from config.features import ITERATION_BUDGET

    restore_eval_budget()
    monkeypatch.setitem(ITERATION_BUDGET, "main_agent_max_iterations", 180)
    pin_eval_budget(200)
    pin_eval_budget(400)  # e.g. a second suite with a different env
    restore_eval_budget()
    assert ITERATION_BUDGET["main_agent_max_iterations"] == 180


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("300", 300), ("0", 0), ("abc", 0), ("-5", 0), ("  250 ", 250), ("", 0)],
)
def test_resolve_eval_iteration_budget(monkeypatch, raw: str, expected: int):
    monkeypatch.setenv("SHERRY_EVAL_MAX_ITERATIONS", raw)
    assert resolve_eval_iteration_budget() == expected


def test_resolve_eval_iteration_budget_default(monkeypatch):
    monkeypatch.delenv("SHERRY_EVAL_MAX_ITERATIONS", raising=False)
    assert resolve_eval_iteration_budget() == 0


# ── session YOLO ────────────────────────────────────────────────────────────


def test_set_session_yolo_delegates_and_skips_empty(monkeypatch):
    import agent.middlewares.humanInTheLoop.approval as approval

    from evals.shared.eval_session import set_session_yolo

    granted: list[str] = []
    monkeypatch.setattr(approval, "set_session_yolo", granted.append, raising=False)
    set_session_yolo("evals-devbench-task-1")
    set_session_yolo("")
    assert granted == ["evals-devbench-task-1"]


def test_set_session_experience_extraction_writes_only_session_override(monkeypatch):
    from evals.shared.eval_session import set_session_experience_extraction

    writes: list[tuple[str, object, object]] = []
    deletes: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "server.service.session_settings_service._write_value",
        lambda sid, key, value: writes.append((sid, key, value)),
    )
    monkeypatch.setattr(
        "server.service.session_settings_service._delete_value",
        lambda sid, key: deletes.append((sid, key)),
    )

    set_session_experience_extraction("evals-deepswe-l1", enabled=True)
    set_session_experience_extraction("", enabled=True)

    assert len(writes) == 1
    assert writes[0][0] == "evals-deepswe-l1"
    assert writes[0][2] == {"skill_extraction_enabled": True}
    assert len(deletes) == 1
    assert deletes[0][0] == "evals-deepswe-l1"


def test_experience_extraction_evidence_reads_mem_first(monkeypatch):
    import runtime

    from evals.shared.eval_session import experience_extraction_evidence

    class _Register:
        def __init__(self, values: dict[str, object]) -> None:
            self.values = values

        def get_state(self, _sid: str, key: object, default: object) -> object:
            return self.values.get(str(key), default)

    monkeypatch.setattr(
        runtime,
        "state_register_mem",
        _Register({"skill_review_fired": True, "skill_review_snapshot_hash": "abc"}),
    )
    monkeypatch.setattr(
        runtime,
        "state_register_db",
        _Register({"skill_review_fired": False, "skill_review_snapshot_hash": "db-hash"}),
    )

    assert experience_extraction_evidence("evals-deepswe-l1") == {
        "fired": True,
        "snapshot_hash": "abc",
    }
    assert experience_extraction_evidence("") == {"fired": False, "snapshot_hash": ""}


def test_force_experience_extraction_runs_product_pass_and_marks_fired(monkeypatch):
    import agent.middlewares.summarization.background_reviews as background_reviews
    import agent.middlewares.summarization.skill_review_runner as runner
    import runtime
    from evals.shared.eval_session import force_experience_extraction
    from runtime.session.state_keys import StateKey

    class _Register:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def set_state(self, _sid: str, key: object, value: object) -> None:
            self.values[str(key)] = value

        def get_state(self, _sid: str, key: object, default: object = None) -> object:
            return self.values.get(str(key), default)

    mem, db = _Register(), _Register()
    monkeypatch.setattr(runtime, "state_register_mem", mem)
    monkeypatch.setattr(runtime, "state_register_db", db)

    calls: list[str] = []

    async def _fake_skill_review(session_id: str) -> None:
        calls.append(session_id)
        db.set_state(session_id, StateKey.SKILL_REVIEW_SNAPSHOT_HASH, "forced-hash")

    monkeypatch.setattr(runner, "_run_skill_review", _fake_skill_review)

    evidence = asyncio.run(force_experience_extraction("evals-deepswe-l1"))

    assert calls == ["evals-deepswe-l1"]
    assert evidence == {"fired": True, "snapshot_hash": "forced-hash"}
    assert db.values[str(StateKey.SKILL_REVIEW_FIRED)] is True
    assert mem.values[str(background_reviews._SKILL_REVIEW_LOCK_KEY)] is False


def test_force_experience_extraction_fails_open(monkeypatch):
    import agent.middlewares.summarization.skill_review_runner as runner
    import runtime
    from evals.shared.eval_session import force_experience_extraction

    class _Register:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def set_state(self, _sid: str, key: object, value: object) -> None:
            self.values[str(key)] = value

        def get_state(self, _sid: str, key: object, default: object = None) -> object:
            return self.values.get(str(key), default)

    monkeypatch.setattr(runtime, "state_register_mem", _Register())
    monkeypatch.setattr(runtime, "state_register_db", _Register())

    async def _boom(_session_id: str) -> None:
        raise RuntimeError("forced review exploded")

    monkeypatch.setattr(runner, "_run_skill_review", _boom)

    assert asyncio.run(force_experience_extraction("evals-deepswe-l1")) == {
        "fired": True,
        "snapshot_hash": "",
    }


def test_force_experience_extraction_skips_empty_session():
    from evals.shared.eval_session import force_experience_extraction

    assert asyncio.run(force_experience_extraction("")) == {"fired": False, "snapshot_hash": ""}


def test_bind_external_session_grants_yolo(monkeypatch, tmp_path: Path):
    """The container binding path must not leave HITL approvals off."""
    import asyncio

    import evals.external_bench as external_bench
    from evals.coding_capability import suite as harness

    granted: list[str] = []

    async def _fake_bind(session_id, repo):
        return None

    def _fake_invalidate(session_id):
        return None

    monkeypatch.setattr(harness, "_bind_session", _fake_bind, raising=False)
    monkeypatch.setattr(harness, "_coding_preset_payload", lambda: {"skills": []}, raising=False)
    monkeypatch.setattr(
        "server.service.agent_config_service.invalidate_session_prompt",
        _fake_invalidate,
        raising=False,
    )
    monkeypatch.setattr(
        "server.service.session_settings_service._write_value", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "server.service.session_settings_service._delete_value", lambda *a, **k: None
    )
    monkeypatch.setattr("evals.shared.eval_session.set_session_yolo", granted.append, raising=False)

    asyncio.run(external_bench.bind_external_session("evals-devbench-x", tmp_path))
    assert granted == ["evals-devbench-x"]


# ── acceptance checklist ────────────────────────────────────────────────────


def test_acceptance_checklist_shared_matches_deepswe_alias():
    from evals.deepswe import suite as deepswe

    instruction = (
        "Responses must raise DecodingError unless the content type matches. "
        "Matching is case-insensitive. The +json suffix applies only to application types."
    )
    assert acceptance_checklist(instruction) == deepswe._acceptance_checklist(instruction)


def test_acceptance_checklist_empty_and_limit():
    assert acceptance_checklist("") == []
    text = " ".join(f"Item {i} must return the normalized value." for i in range(20))
    assert len(acceptance_checklist(text)) == 8
    assert len(acceptance_checklist(text, limit=3)) == 3


# ── continuation cap ────────────────────────────────────────────────────────


def test_devbench_continuation_cap_matches_deepswe():
    from evals.devbench import suite as devbench
    from evals.deepswe import suite as deepswe

    assert devbench._MAX_PLAN_CONTINUATIONS == 3
    assert devbench._MAX_PLAN_CONTINUATIONS == deepswe._MAX_PLAN_CONTINUATIONS


# ── delegation pressure eval opt-in ─────────────────────────────────────────


def test_delegation_pressure_eval_opt_in_no_longer_needs_legacy_prefix(monkeypatch):
    from agent.middlewares.delegation_pressure import core

    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "1")
    # A modern suite prefix, which the old ``evals-swe-`` check never matched.
    assert core._active("evals-deepswe-task-1") is True
    # Child / swarm sessions are still excluded regardless of the env var.
    assert core._active("evals-deepswe-task-1:subagent:0") is False
    assert core._active("evals-deepswe-task-1:swarm:0") is False


def test_delegation_pressure_product_default_unchanged(monkeypatch):
    from agent.middlewares.delegation_pressure import core

    monkeypatch.delenv("SHERRY_EVAL_DELEGATION_PRESSURE", raising=False)
    monkeypatch.setitem(core.DELEGATION_PRESSURE, "enabled", True)
    assert core._active("user-session") is True
    monkeypatch.setitem(core.DELEGATION_PRESSURE, "enabled", False)
    assert core._active("user-session") is False
