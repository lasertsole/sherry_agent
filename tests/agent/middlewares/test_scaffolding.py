"""Tests for middleware scaffolding protection (P1-6).

Mirrors deepagents' ``test_graph.py::TestRequiredMiddlewareNamesCoverage`` and
its exclusion-wiring tests, adapted for Sherry's dual-chain model. The
``TestActualChainCompliance`` block is the real drift guard: it feeds the
*actual* lists produced by ``agent/core.py::_build_middlewares`` and
``spawn/core.py::_build_child_middlewares`` through the validator, so removing
a safety-critical middleware from either chain turns this file red.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.core import _build_middlewares
from agent.middlewares import (
    HeartbeatStaleness,
    IterationBudget,
    MaxTokensBoostMiddleware,
    OutputRepetitionGuard,
    Summarization,
    ToolCallNormalize,
    ToolGuardrails,
)
from agent.middlewares.scaffolding import (
    MAIN_REQUIRED_CLASSES,
    MAIN_REQUIRED_NAMES,
    SUBAGENT_REQUIRED_CLASSES,
    SUBAGENT_REQUIRED_NAMES,
    RequiredMiddlewareEntry,
    ScaffoldingViolationError,
    _MAIN_REQUIRED,
    _SUBAGENT_REQUIRED,
    validate_required_middleware,
    verify_required_names_coverage,
)
from agent.tools.subagent.spawn.core import _build_child_middlewares

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_CONTEXT_WINDOW = 131_072


class _AliasMiddleware:
    """A non-required stand-in exposing only a custom ``.name`` alias."""

    def __init__(self, alias: str) -> None:
        self._alias = alias

    @property
    def name(self) -> str:
        return self._alias


def _real_subagent_chain() -> list[Any]:
    return _build_child_middlewares(
        auxiliary_llm=None,
        main_llm_context_window=_CONTEXT_WINDOW,
    )


class TestRequiredNamesCoverage:
    """Drift guard: the derived ``*_REQUIRED_NAMES`` cover every entry."""

    def test_main_names_cover_all_classes(self):
        for entry in _MAIN_REQUIRED:
            assert entry.cls.__name__ in MAIN_REQUIRED_NAMES
            for alias in entry.names:
                assert alias in MAIN_REQUIRED_NAMES

    def test_subagent_names_cover_all_classes(self):
        for entry in _SUBAGENT_REQUIRED:
            assert entry.cls.__name__ in SUBAGENT_REQUIRED_NAMES
            for alias in entry.names:
                assert alias in SUBAGENT_REQUIRED_NAMES

    def test_verify_required_names_coverage_passes(self):
        verify_required_names_coverage()


class TestValidateRequiredMiddleware:
    """Branch coverage for validate_required_middleware()."""

    def test_empty_list_raises(self):
        with pytest.raises(ScaffoldingViolationError, match="empty"):
            validate_required_middleware([], chain="main", entries=_MAIN_REQUIRED)

    def test_missing_class_raises(self):
        incomplete = [
            Summarization(model=None),
            IterationBudget(60),
            OutputRepetitionGuard(),
            MaxTokensBoostMiddleware(),
            ToolCallNormalize(),
            HeartbeatStaleness(),
        ]
        with pytest.raises(ScaffoldingViolationError, match="ToolGuardrails"):
            validate_required_middleware(incomplete, chain="subagent", entries=_SUBAGENT_REQUIRED)

    def test_rejection_message_is_actionable(self):
        with pytest.raises(ScaffoldingViolationError) as excinfo:
            validate_required_middleware(
                [IterationBudget(60)], chain="subagent", entries=_SUBAGENT_REQUIRED
            )
        message = str(excinfo.value)
        assert "subagent" in message
        assert "_SUBAGENT_REQUIRED" in message
        assert "scaffolding.py" in message

    def test_missing_name_raises(self):
        entry = RequiredMiddlewareEntry(ToolGuardrails, ("ToolGuardrailsAlias",))
        with pytest.raises(ScaffoldingViolationError, match="ToolGuardrails"):
            validate_required_middleware([IterationBudget(60)], chain="subagent", entries=(entry,))

    def test_alias_name_satisfies_requirement(self):
        entry = RequiredMiddlewareEntry(ToolGuardrails, ("ToolGuardrailsAlias",))
        validate_required_middleware(
            [_AliasMiddleware("ToolGuardrailsAlias")],
            chain="subagent",
            entries=(entry,),
        )

    def test_subclass_does_not_satisfy_parent_requirement(self):
        class _CustomToolGuardrails(ToolGuardrails):
            pass

        with pytest.raises(ScaffoldingViolationError):
            validate_required_middleware(
                [_CustomToolGuardrails()], chain="subagent", entries=_SUBAGENT_REQUIRED
            )

    def test_all_present_passes(self):
        validate_required_middleware(
            _real_subagent_chain(), chain="subagent", entries=_SUBAGENT_REQUIRED
        )


class TestActualChainCompliance:
    """Real-chain drift guard: the assembled lists must pass validation."""

    def test_main_agent_middleware_list_passes(self):
        middleware = _build_middlewares(
            fallback_chain=None,
            auxiliary_llm=None,
            main_llm_context_window=_CONTEXT_WINDOW,
            compression_trigger_ratio=0.8,
        )
        validate_required_middleware(middleware, chain="main", entries=_MAIN_REQUIRED)
        assert MAIN_REQUIRED_CLASSES <= {type(mw) for mw in middleware}

    def test_subagent_middleware_list_passes(self):
        middleware = _real_subagent_chain()
        validate_required_middleware(middleware, chain="subagent", entries=_SUBAGENT_REQUIRED)
        assert SUBAGENT_REQUIRED_CLASSES <= {type(mw) for mw in middleware}

    @pytest.mark.asyncio
    async def test_build_graph_validates_before_create_agent(self, monkeypatch):
        """The main graph must fail the build, not call create_agent, when a
        required middleware is missing."""
        from agent import core as agent_core

        class _Checkpointer:
            async def setup(self) -> None:
                return None

            async def aclean_old_checkpoints(self) -> None:
                return None

        async def _checkpointer() -> _Checkpointer:
            return _Checkpointer()

        monkeypatch.setattr(agent_core, "build_async_sqlite_checkpointer", _checkpointer)
        monkeypatch.setattr(agent_core, "build_main_llm", lambda: object())
        monkeypatch.setattr(agent_core, "build_auxiliary_llm", lambda: object())
        monkeypatch.setattr(agent_core, "build_fallback_chain", lambda: object())
        monkeypatch.setattr(agent_core, "get_agent_tools", lambda: [])
        monkeypatch.setattr(agent_core, "_build_middlewares", lambda **_: [])

        create_agent_calls: list[dict[str, Any]] = []

        def _create_agent(**kwargs: Any) -> object:
            create_agent_calls.append(kwargs)
            return object()

        monkeypatch.setattr(agent_core, "create_agent", _create_agent)

        with pytest.raises(ScaffoldingViolationError, match="empty"):
            await agent_core._build_graph(
                temperature=0.8,
                main_llm_context_window=_CONTEXT_WINDOW,
                compression_trigger_ratio=0.8,
            )
        assert create_agent_calls == []


class TestRoleAgnosticDesign:
    """The single _SUBAGENT_REQUIRED set applies to every child role."""

    def test_subagent_required_is_subset_of_main_required(self):
        assert SUBAGENT_REQUIRED_CLASSES.issubset(MAIN_REQUIRED_CLASSES)

    def test_main_only_middleware_not_in_subagent_required(self):
        expected_main_only = {
            "HumanInTheLoop",
            "MessagePersistenceMiddleware",
            "ContextEvictionMiddleware",
            "PathGuard",
            "LLMRetryMiddleware",
        }
        main_only = MAIN_REQUIRED_CLASSES - SUBAGENT_REQUIRED_CLASSES
        assert {cls.__name__ for cls in main_only} == expected_main_only

    def test_subagent_required_matches_derived_classes(self):
        assert SUBAGENT_REQUIRED_CLASSES == frozenset(e.cls for e in _SUBAGENT_REQUIRED)
