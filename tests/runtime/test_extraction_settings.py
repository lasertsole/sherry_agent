"""Unit tests for ``runtime/session/extraction.py`` — the effective-config reader.

The gates (skill review, PTC/LDW compile, IterationBudget) all read through
:func:`effective_extraction_config`, so this suite pins the resolution order
(session override → global → default), the global-only flow rule, the
parent/child force-off, the clamps and the fail-open behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

import runtime.session.extraction as extraction

pytestmark = [pytest.mark.unit]


class _FakeRegister:
    """Minimal stand-in for a state register (mem or DB tier)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.data[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.data.pop((session_id, key), None)
        return True


@pytest.fixture()
def registers(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeRegister, _FakeRegister]:
    """Patch both register tiers the module reads from."""
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(extraction, "state_register_mem", mem)
    monkeypatch.setattr(extraction, "state_register_db", db)
    return mem, db


@pytest.fixture()
def global_values(monkeypatch: pytest.MonkeyPatch):
    """Install the global sherry.jsonc row the module reads (dotted keys)."""

    def _set(
        *,
        skill: bool = True,
        flow: bool = True,
        main: int = 180,
        sub: int = 90,
    ) -> None:
        monkeypatch.setattr(
            extraction,
            "load_sherry_settings",
            lambda: {
                "extraction.skill_extraction_enabled": skill,
                "extraction.flow_extraction_enabled": flow,
                "extraction.iteration_budget_main": main,
                "extraction.iteration_budget_sub": sub,
            },
        )

    return _set


class TestGlobalConfig:
    def test_missing_settings_read_as_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> dict:
            raise RuntimeError("config file is half-initialized")

        monkeypatch.setattr(extraction, "load_sherry_settings", _boom)
        assert extraction.global_extraction_config() == extraction.EXTRACTION_DEFAULTS

    def test_values_are_clamped_and_sub_follows_main(self, global_values) -> None:
        global_values(main=0, sub=5000)
        values = extraction.global_extraction_config()
        # main 0 would stop the agent before its first call: floored to 1, and
        # the sub ceiling can never exceed the main one.
        assert values["iteration_budget_main"] == 1
        assert values["iteration_budget_sub"] == 1

    def test_sub_is_capped_at_main(self, global_values) -> None:
        global_values(main=40, sub=90)
        assert extraction.global_extraction_config()["iteration_budget_sub"] == 40


class TestSessionOverrides:
    def test_no_session_reads_global(self, registers, global_values) -> None:
        global_values(skill=False, flow=False, main=30, sub=20)
        values = extraction.effective_extraction_config("")
        assert values["skill_extraction_enabled"] is False
        assert values["flow_extraction_enabled"] is False
        assert values["iteration_budget_main"] == 30
        assert values["iteration_budget_sub"] == 20

    def test_session_overrides_win_per_field(self, registers, global_values) -> None:
        mem, _db = registers
        global_values(skill=True, flow=True, main=180, sub=90)
        mem.set_state(
            "s1",
            extraction.StateKey.EXTRACTION,
            {"skill_extraction_enabled": False, "iteration_budget_main": 40},
        )
        values = extraction.effective_extraction_config("s1")
        assert values["skill_extraction_enabled"] is False
        # Parent off forces the child off, whatever the global flow value says.
        assert values["flow_extraction_enabled"] is False
        assert values["iteration_budget_main"] == 40
        # The untouched sub ceiling still inherits the global value (clamped).
        assert values["iteration_budget_sub"] == 40

    def test_db_fallback_rehydrates_mem(self, registers, global_values) -> None:
        mem, db = registers
        global_values()
        db.set_state(
            "s2",
            extraction.StateKey.EXTRACTION,
            {"iteration_budget_main": 77},
        )
        assert extraction.session_extraction_overrides("s2") == {"iteration_budget_main": 77}
        assert mem.get_state("s2", extraction.StateKey.EXTRACTION) == {"iteration_budget_main": 77}

    def test_malformed_stored_payload_is_ignored(self, registers, global_values) -> None:
        mem, _db = registers
        global_values()
        # A flow key in session state, a string ceiling and a bool ceiling are
        # all dropped by the normalizer; the rest of the payload survives.
        mem.set_state(
            "s3",
            extraction.StateKey.EXTRACTION,
            {
                "flow_extraction_enabled": False,
                "iteration_budget_main": "200",
                "iteration_budget_sub": True,
                "skill_extraction_enabled": False,
            },
        )
        assert extraction.session_extraction_overrides("s3") == {"skill_extraction_enabled": False}

    def test_register_failure_falls_back_to_global(
        self, monkeypatch: pytest.MonkeyPatch, global_values
    ) -> None:
        global_values(skill=True, flow=True)

        class _Broken:
            def get_state(self, *args, **kwargs):
                raise RuntimeError("register is gone")

            def set_state(self, *args, **kwargs):
                raise RuntimeError("register is gone")

        monkeypatch.setattr(extraction, "state_register_mem", _Broken())
        monkeypatch.setattr(extraction, "state_register_db", _Broken())
        assert extraction.effective_extraction_config("s4")["skill_extraction_enabled"] is True


class TestSanitizer:
    def test_session_scope_rejects_flow_by_name(self) -> None:
        with pytest.raises(extraction.ExtractionSettingsError, match="global setting"):
            extraction.sanitize_extraction_values(
                {"flow_extraction_enabled": False}, scope="session"
            )

    def test_global_scope_accepts_all_four(self) -> None:
        cleaned = extraction.sanitize_extraction_values(
            {
                "skill_extraction_enabled": False,
                "flow_extraction_enabled": True,
                "iteration_budget_main": 200,
                "iteration_budget_sub": 150,
            },
            scope="global",
        )
        assert cleaned["iteration_budget_sub"] == 150

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(extraction.ExtractionSettingsError, match="unknown field"):
            extraction.sanitize_extraction_values({"iteration_budget_grand": 5}, scope="session")

    def test_null_fields_are_dropped_and_empty_is_legal(self) -> None:
        cleaned = extraction.sanitize_extraction_values(
            {"skill_extraction_enabled": None, "iteration_budget_main": None}, scope="session"
        )
        assert cleaned == {}
        # An empty payload is the explicit "inherit everything" choice.
        assert extraction.sanitize_extraction_values({}, scope="session") == {}

    def test_iteration_bounds_and_boolean_types(self) -> None:
        with pytest.raises(extraction.ExtractionSettingsError, match="between"):
            extraction.sanitize_extraction_values({"iteration_budget_main": 0}, scope="session")
        with pytest.raises(extraction.ExtractionSettingsError, match="integer"):
            extraction.sanitize_extraction_values({"iteration_budget_main": True}, scope="session")
        with pytest.raises(extraction.ExtractionSettingsError, match="boolean"):
            extraction.sanitize_extraction_values(
                {"skill_extraction_enabled": "yes"}, scope="session"
            )

    def test_sub_is_clamped_to_main(self) -> None:
        cleaned = extraction.sanitize_extraction_values(
            {"iteration_budget_main": 50, "iteration_budget_sub": 80}, scope="session"
        )
        assert cleaned["iteration_budget_sub"] == 50


class TestRootSessionId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("main-1", "main-1"),
            ("agent:main-1:subagent:abc", "main-1"),
            ("agent:main-1:swarm:g:abc", "main-1"),
            ("agent:main:session:main-1", "agent:main:session:main-1"),
            ("  ", ""),
        ],
    )
    def test_root_extraction(self, raw: str, expected: str) -> None:
        assert extraction.root_session_id(raw) == expected


class TestFlowShorthand:
    def test_disabled_when_skill_is_off(self, registers, global_values) -> None:
        global_values(skill=False, flow=True)
        assert extraction.flow_extraction_enabled("") is False

    def test_enabled_by_default(self, registers, global_values) -> None:
        global_values(skill=True, flow=True)
        assert extraction.flow_extraction_enabled("") is True
