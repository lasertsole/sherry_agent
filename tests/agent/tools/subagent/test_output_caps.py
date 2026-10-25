"""Subagent output caps scale with the serving model's configured max output."""

from __future__ import annotations

import pytest

from agent.tools.subagent.announce.output import build_child_completion_findings
from agent.tools.subagent.output_caps import (
    resolve_child_max_output_tokens,
    resolve_subagent_output_caps,
)
from agent.tools.subagent.types.registry import SubagentRunRecord

pytestmark = [pytest.mark.unit]


def test_unset_keeps_the_legacy_caps_exactly():
    caps = resolve_subagent_output_caps(None)

    assert caps.stored_bytes == 24000
    assert caps.delivery_chars == 4000
    assert caps.summary_chars == 2000
    assert caps.judge_chars == 8000
    assert caps.transcript_tokens == 4000
    assert caps.per_message_tokens == 500


def test_the_8192_setting_is_the_micro_adjusted_baseline():
    caps = resolve_subagent_output_caps(8192)

    assert caps.stored_bytes == 24576
    assert caps.delivery_chars == 4096
    assert caps.summary_chars == 2048
    assert caps.judge_chars == 8192
    assert caps.transcript_tokens == 4096
    assert caps.per_message_tokens == 512


def test_larger_output_windows_scale_linearly_from_8000():
    caps = resolve_subagent_output_caps(16384)

    assert caps.stored_bytes == 49152
    assert caps.delivery_chars == 8192
    assert caps.summary_chars == 4096
    assert caps.judge_chars == 16384
    assert caps.transcript_tokens == 8192
    assert caps.per_message_tokens == 1024


@pytest.mark.parametrize("value", [0, -1, 1.5, "8192"])
def test_non_positive_or_non_integer_values_keep_the_legacy_caps(value):
    caps = resolve_subagent_output_caps(value)  # type: ignore[arg-type]

    assert caps.stored_bytes == 24000
    assert caps.delivery_chars == 4000


def test_every_scaled_cap_stays_at_least_one():
    caps = resolve_subagent_output_caps(1)

    assert caps.stored_bytes == 3
    assert caps.delivery_chars == 1
    assert caps.summary_chars == 1
    assert caps.judge_chars == 1
    assert caps.transcript_tokens == 1
    assert caps.per_message_tokens == 1


@pytest.mark.parametrize(
    ("kwargs", "expected_key"),
    [
        ({"model_profile": {"model": "m"}}, "MAIN_LLM_MAX_OUTPUT_TOKEN"),
        (
            {"functional_role": "researcher", "model_tier": "auxiliary"},
            "AUXILIARY_LLM_MAX_OUTPUT_TOKEN",
        ),
        ({"functional_role": "researcher", "model_tier": "main"}, "MAIN_LLM_MAX_OUTPUT_TOKEN"),
        ({"role": "orchestrator"}, "MAIN_LLM_MAX_OUTPUT_TOKEN"),
        ({"role": "leaf"}, "AUXILIARY_LLM_MAX_OUTPUT_TOKEN"),
    ],
)
def test_model_selection_mirrors_the_child_builder(monkeypatch, kwargs, expected_key):
    from agent.tools.subagent.types.capability import SubagentSessionRole
    from agent.tools.subagent.types.functional_role import FunctionalRole

    seen: list[str] = []

    def fake_configured(key: str) -> int | None:
        seen.append(key)
        return 16384

    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", fake_configured)

    values = {
        "model_profile": kwargs.get("model_profile"),
        "model_override": kwargs.get("model_override"),
        "functional_role": (
            FunctionalRole(kwargs["functional_role"])
            if kwargs.get("functional_role")
            else FunctionalRole.GENERAL
        ),
        "model_tier": kwargs.get("model_tier"),
        "role": (
            SubagentSessionRole(kwargs["role"]) if kwargs.get("role") else SubagentSessionRole.LEAF
        ),
    }

    assert resolve_child_max_output_tokens(**values) == 16384
    assert seen == [expected_key]


def test_a_named_model_override_has_no_known_budget(monkeypatch):
    called = False

    def fake_configured(key: str) -> int | None:
        nonlocal called
        called = True
        return 16384

    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", fake_configured)

    assert (
        resolve_child_max_output_tokens(
            model_profile=None,
            model_override="some-explicit-model",
            functional_role="general",
            model_tier=None,
            role="leaf",
        )
        is None
    )
    assert called is False


def test_a_profile_without_a_model_does_not_shadow_the_override(monkeypatch):
    called = False

    def fake_configured(key: str) -> int | None:
        nonlocal called
        called = True
        return 16384

    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", fake_configured)

    assert (
        resolve_child_max_output_tokens(
            model_profile={"provider": "openai"},
            model_override="some-explicit-model",
            functional_role="general",
            model_tier=None,
            role="leaf",
        )
        is None
    )
    assert called is False


def test_a_general_role_ignores_a_tier_hint(monkeypatch):
    seen: list[str] = []

    def fake_configured(key: str) -> int | None:
        seen.append(key)
        return 16384

    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", fake_configured)

    assert (
        resolve_child_max_output_tokens(
            model_profile=None,
            model_override=None,
            functional_role="general",
            model_tier="main",
            role="leaf",
        )
        == 16384
    )
    assert seen == ["AUXILIARY_LLM_MAX_OUTPUT_TOKEN"]


def test_an_unset_model_setting_keeps_the_legacy_caps(monkeypatch):
    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", lambda key: None)

    assert (
        resolve_child_max_output_tokens(
            model_profile=None,
            model_override=None,
            functional_role="general",
            model_tier=None,
            role="leaf",
        )
        is None
    )


def test_an_invalid_env_value_falls_back_to_legacy(monkeypatch):
    def boom(key: str) -> int | None:
        raise ValueError("bad value")

    monkeypatch.setattr("models.LLMs.provider_max_output.configured_max_output", boom)

    assert (
        resolve_child_max_output_tokens(
            model_profile=None,
            model_override=None,
            functional_role="general",
            model_tier=None,
            role="leaf",
        )
        is None
    )


def test_completion_summary_uses_the_scaled_cap():
    run = SubagentRunRecord(
        run_id="r1",
        child_session_key="child",
        requester_session_key="parent",
        task="task",
        max_output_tokens=16384,
        completion={"result_text": "x" * 5000},  # type: ignore[arg-type]
    )

    findings = build_child_completion_findings(run)

    assert "x" * 4096 in findings
    assert "x" * 5000 not in findings


def test_completion_summary_keeps_the_legacy_cap_when_unset():
    run = SubagentRunRecord(
        run_id="r1",
        child_session_key="child",
        requester_session_key="parent",
        task="task",
        completion={"result_text": "x" * 2500},  # type: ignore[arg-type]
    )

    findings = build_child_completion_findings(run)

    assert "x" * 2000 in findings
    assert "x" * 2500 not in findings


def test_the_run_budget_survives_the_registry_roundtrip():
    from agent.tools.subagent.registry.store_sqlite import _deserialize_run, _serialize_run

    run = SubagentRunRecord(
        run_id="r1",
        child_session_key="child",
        requester_session_key="parent",
        task="task",
        max_output_tokens=16384,
    )

    assert _deserialize_run(_serialize_run(run)).max_output_tokens == 16384


def test_a_legacy_run_row_without_the_field_reads_as_unset():
    restored = SubagentRunRecord.model_validate(
        {
            "run_id": "legacy",
            "child_session_key": "child",
            "requester_session_key": "parent",
            "task": "task",
        }
    )

    assert restored.max_output_tokens is None
    assert resolve_subagent_output_caps(restored.max_output_tokens).stored_bytes == 24000
