"""Per-role model families in the child-LLM precedence chain.

A CONFIGURED ``EXECUTOR_LLM_*`` (etc.) family outranks the role's model tier; an
EMPTY family leaves the existing tier/depth behavior untouched, so a deployment
that never touches the new keys is byte-for-byte unchanged. The output-cap and
structured-output identity mirrors must agree with that precedence.
"""

from __future__ import annotations

import pytest

from agent.tools.subagent.output_caps import resolve_child_max_output_tokens
from agent.tools.subagent.spawn.child_agent_factory import (
    _child_model_identity,
    _child_model_source,
    _select_child_llm,
)
from agent.tools.subagent.types.capability import SubagentSessionRole
from agent.tools.subagent.types.functional_role import FunctionalRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


@pytest.fixture
def clean_family(monkeypatch):
    """No role family configured unless the test says so."""
    import models.LLMs.role_llm as role_llm

    monkeypatch.setattr(role_llm, "role_llm_configured", lambda _role: False)
    return monkeypatch


def _patch_role_family(monkeypatch, *, configured: bool, llm: object):
    import models.LLMs.role_llm as role_llm

    monkeypatch.setattr(role_llm, "role_llm_configured", lambda _role: configured)
    monkeypatch.setattr(role_llm, "build_role_llm", lambda _role, temperature=None: llm)


def test_an_unconfigured_role_family_keeps_the_role_tier(clean_family):
    aux = object()
    clean_family.setattr("models.build_auxiliary_llm", lambda temperature=None: aux)

    llm = _select_child_llm(
        None, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
    )

    assert llm is aux
    assert (
        _child_model_source(
            None, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
        )
        == "tier_auxiliary"
    )


def test_a_configured_role_family_outranks_the_tier(clean_family):
    family_llm = object()
    _patch_role_family(clean_family, configured=True, llm=family_llm)
    clean_family.setattr(
        "models.build_auxiliary_llm",
        lambda temperature=None: pytest.fail("the role family must win over the tier"),
    )

    llm = _select_child_llm(
        None, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
    )

    assert llm is family_llm
    assert (
        _child_model_source(
            None, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
        )
        == "role_family"
    )


def test_a_role_profile_still_outranks_the_family(clean_family):
    profile = {"model": "claude-sonnet", "provider": "anthropic"}
    import models.LLMs.role_llm as role_llm

    clean_family.setattr(role_llm, "role_llm_configured", lambda _role: True)
    clean_family.setattr(
        role_llm, "build_role_llm", lambda *_a, **_k: pytest.fail("profile must win")
    )

    built: list[dict] = []

    def _build_profile(**kwargs):
        built.append(kwargs)
        return object()

    clean_family.setattr("models.build_main_llm_for_profile", _build_profile)

    _select_child_llm(profile, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF)

    assert built and built[0]["model"] == "claude-sonnet"
    assert (
        _child_model_source(
            profile, None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
        )
        == "role_profile"
    )


def test_the_identity_role_never_reaches_a_family(clean_family):
    import models.LLMs.role_llm as role_llm

    clean_family.setattr(role_llm, "role_llm_configured", lambda _role: True)
    clean_family.setattr(
        role_llm, "build_role_llm", lambda *_a, **_k: pytest.fail("general has no family")
    )
    main = object()
    clean_family.setattr("models.build_main_llm", lambda temperature=None, **kwargs: main)

    llm = _select_child_llm(
        None, None, FunctionalRole.GENERAL, None, SubagentSessionRole.ORCHESTRATOR
    )

    assert llm is main
    assert (
        _child_model_source(None, None, FunctionalRole.GENERAL, None, SubagentSessionRole.LEAF)
        == "depth_auxiliary"
    )


def test_configured_family_output_caps_use_the_family_key(clean_family):
    monkeypatch = clean_family
    _patch_role_family(monkeypatch, configured=True, llm=object())
    monkeypatch.setenv("EXECUTOR_LLM_MAX_OUTPUT_TOKEN", "16384")

    value = resolve_child_max_output_tokens(
        model_profile=None,
        model_override=None,
        functional_role=FunctionalRole.EXECUTOR,
        model_tier="auxiliary",
        role=SubagentSessionRole.LEAF,
    )

    assert value == 16384


def test_unconfigured_family_output_caps_keep_the_tier_key(clean_family):
    monkeypatch = clean_family
    monkeypatch.setenv("AUXILIARY_LLM_MAX_OUTPUT_TOKEN", "8192")
    monkeypatch.setenv("EXECUTOR_LLM_MAX_OUTPUT_TOKEN", "16384")

    value = resolve_child_max_output_tokens(
        model_profile=None,
        model_override=None,
        functional_role=FunctionalRole.EXECUTOR,
        model_tier="auxiliary",
        role=SubagentSessionRole.LEAF,
    )

    assert value == 8192


def test_configured_family_identity_feeds_the_structured_output_lookup(clean_family):
    monkeypatch = clean_family
    _patch_role_family(monkeypatch, configured=True, llm=object())
    monkeypatch.setenv("EXECUTOR_LLM_PROVIDER", "openai")
    monkeypatch.setenv("EXECUTOR_LLM_API_NAME", "gpt-4o-mini")

    provider, model = _child_model_identity(
        None, FunctionalRole.EXECUTOR, "auxiliary", SubagentSessionRole.LEAF
    )

    assert (provider, model) == ("openai", "gpt-4o-mini")
