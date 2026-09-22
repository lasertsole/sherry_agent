"""Middleware scaffolding protection — required-middleware enforcement.

Adapted from deepagents' three-layer defense (``graph.py::_REQUIRED_MIDDLEWARE``
and ``_excluded_middleware.py::_validate_excluded_middleware_config``),
simplified for Sherry's hardcoded middleware lists (no ``HarnessProfile`` /
``excluded_middleware`` mechanism yet).

Two required sets:

* ``_MAIN_REQUIRED`` — the safety / reliability baseline for the main agent
  chain assembled by ``agent/core.py::_build_middlewares``.
* ``_SUBAGENT_REQUIRED`` — the safety baseline for the child-agent chain
  assembled by ``agent/tools/subagent/spawn/core.py::_build_child_middlewares``.

``validate_required_middleware()`` is called at both assembly points, *before*
``create_agent(...)``, so an accidentally removed required middleware fails the
build immediately instead of shipping silently. It is a startup-time assertion
only — it never runs on the per-turn / per-call hot path.

The required classes are imported from their concrete submodules rather than
from the ``agent.middlewares`` package: ``agent/middlewares/__init__.py``
re-exports this module at the bottom, so importing the package back from here
would create a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from loguru import logger

from .context_eviction import ContextEvictionMiddleware
from .heartbeat_staleness import HeartbeatStaleness
from .humanInTheLoop import HumanInTheLoop
from .iteration_budget import IterationBudget
from .llm_retry import LLMRetryMiddleware
from .max_tokens_boost import MaxTokensBoostMiddleware
from .message_persistence import MessagePersistenceMiddleware
from .output_repetition_guard import OutputRepetitionGuard
from .path_guard import PathGuard
from .summarization import Summarization
from .tool_call_normalize import ToolCallNormalize
from .tool_guardrails import ToolGuardrails


class RequiredMiddlewareEntry(NamedTuple):
    """A required middleware entry — the class plus accepted string aliases.

    Mirrors deepagents' ``(class, aliases)`` tuple pattern: a required entry is
    satisfied by an exact class match OR by any accepted name (``cls.__name__``
    plus the optional aliases in ``names``).
    """

    cls: type
    names: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Main-agent required middleware
# ---------------------------------------------------------------------------

_MAIN_REQUIRED: tuple[RequiredMiddlewareEntry, ...] = (
    # Tool-call safety guardrails (failure-pathology detection / halt).
    RequiredMiddlewareEntry(ToolGuardrails, ()),
    # Human-in-the-loop approval gate for sensitive operations.
    RequiredMiddlewareEntry(HumanInTheLoop, ()),
    # Context compaction — without it long sessions exhaust the window.
    RequiredMiddlewareEntry(Summarization, ()),
    # Per-turn call budget — without it a runaway loop exhausts tokens.
    RequiredMiddlewareEntry(IterationBudget, ()),
    # Message persistence — without it MesMemory history breaks.
    RequiredMiddlewareEntry(MessagePersistenceMiddleware, ()),
    # Tool-result / human-message eviction — context-overflow defense.
    RequiredMiddlewareEntry(ContextEvictionMiddleware, ()),
    # Path-argument screening for tool calls.
    RequiredMiddlewareEntry(PathGuard, ()),
    # Classified LLM retry + fallback — a single failure must not abort.
    RequiredMiddlewareEntry(LLMRetryMiddleware, ()),
    # Output-repetition detection (loop prevention).
    RequiredMiddlewareEntry(OutputRepetitionGuard, ()),
    # Truncated tool-call recovery via a boosted re-call.
    RequiredMiddlewareEntry(MaxTokensBoostMiddleware, ()),
    # Stuck-turn watchdog.
    RequiredMiddlewareEntry(HeartbeatStaleness, ()),
    # Transcript repair (tool_use / tool_result pairing).
    RequiredMiddlewareEntry(ToolCallNormalize, ()),
)

# ---------------------------------------------------------------------------
# Subagent required middleware
# ---------------------------------------------------------------------------

# The child chain is intentionally leaner than the main chain: no HITL,
# eviction, persistence, path guard, or LLM retry. The entries below are the
# per-agent safety baseline every child shares regardless of depth role
# (ORCHESTRATOR / LEAF) or functional role.
_SUBAGENT_REQUIRED: tuple[RequiredMiddlewareEntry, ...] = (
    RequiredMiddlewareEntry(ToolGuardrails, ()),
    RequiredMiddlewareEntry(IterationBudget, ()),
    RequiredMiddlewareEntry(Summarization, ()),
    RequiredMiddlewareEntry(OutputRepetitionGuard, ()),
    RequiredMiddlewareEntry(HeartbeatStaleness, ()),
    RequiredMiddlewareEntry(ToolCallNormalize, ()),
    RequiredMiddlewareEntry(MaxTokensBoostMiddleware, ()),
)


# Derived sets for fast membership testing (mirrors deepagents'
# ``graph.py:258-268``). ``*_CLASSES`` is the set of required class objects;
# ``*_NAMES`` is the union of every accepted name (class name + aliases).
MAIN_REQUIRED_CLASSES: frozenset[type] = frozenset(e.cls for e in _MAIN_REQUIRED)
MAIN_REQUIRED_NAMES: frozenset[str] = frozenset(
    name for e in _MAIN_REQUIRED for name in (e.cls.__name__, *e.names)
)

SUBAGENT_REQUIRED_CLASSES: frozenset[type] = frozenset(e.cls for e in _SUBAGENT_REQUIRED)
SUBAGENT_REQUIRED_NAMES: frozenset[str] = frozenset(
    name for e in _SUBAGENT_REQUIRED for name in (e.cls.__name__, *e.names)
)


class ScaffoldingViolationError(RuntimeError):
    """Raised when a required middleware is missing from an assembled chain."""


def _format_rejection(missing_names: set[str], chain: str) -> str:
    """Format an actionable rejection message for a scaffolding violation."""
    return (
        f"Required middleware scaffolding violated for {chain} agent chain.\n"
        f"  Missing: {sorted(missing_names)}\n"
        f"  These middleware are safety-critical and cannot be omitted.\n"
        f"  If this is intentional, update _MAIN_REQUIRED / _SUBAGENT_REQUIRED "
        f"in agent/middlewares/scaffolding.py and its tests."
    )


def _extract_middleware_names(middleware_list: Sequence[Any]) -> set[str]:
    """Collect every identifiable name from an assembled middleware list.

    Includes each entry's runtime class name, a ``.name`` attribute when
    present, and a ``serialized_name`` class attribute when present — mirroring
    deepagents' dual class / string matching.
    """
    names: set[str] = set()
    for mw in middleware_list:
        names.add(type(mw).__name__)
        name_attr = getattr(mw, "name", None)
        if isinstance(name_attr, str):
            names.add(name_attr)
        serialized = getattr(type(mw), "serialized_name", None)
        if isinstance(serialized, str):
            names.add(serialized)
    return names


def validate_required_middleware(
    middleware_list: Sequence[Any],
    *,
    chain: str,
    entries: tuple[RequiredMiddlewareEntry, ...],
) -> None:
    """Validate that every required middleware is present in an assembled list.

    Called at both assembly points (``_build_graph`` + ``_build_child_agent``)
    immediately before ``create_agent(...)``. This is the "second defense
    layer": Sherry has no exclusion mechanism yet, so the check catches an
    accidental removal during code changes at build time.

    Args:
        middleware_list: The ``middleware=`` argument passed to ``create_agent``.
        chain: ``"main"`` or ``"subagent"`` — for the error message only.
        entries: The required entries to validate against. Callers pass
            ``_MAIN_REQUIRED`` or ``_SUBAGENT_REQUIRED``.

    Raises:
        ScaffoldingViolationError: If the list is empty or any required
            middleware is missing.
    """
    if not middleware_list:
        raise ScaffoldingViolationError(
            f"Middleware list is empty for {chain} agent chain — "
            f"required scaffolding cannot be validated."
        )

    actual_classes = {type(mw) for mw in middleware_list}
    actual_names = _extract_middleware_names(middleware_list)

    # A required middleware counts as missing only when BOTH its class and all
    # of its accepted names are absent. This tolerates a renamed/aliased class
    # while still rejecting a subclass that merely shadows the parent name.
    missing: set[str] = set()
    for entry in entries:
        cls_present = entry.cls in actual_classes
        name_present = any(n in actual_names for n in (entry.cls.__name__, *entry.names))
        if not cls_present and not name_present:
            missing.add(entry.cls.__name__)

    if missing:
        raise ScaffoldingViolationError(_format_rejection(missing, chain))

    logger.debug(
        "Middleware scaffolding validated for {} chain: {} required, {} actual",
        chain,
        len(entries),
        len(middleware_list),
    )


def verify_required_names_coverage() -> None:
    """Verify the derived ``*_REQUIRED_NAMES`` cover every required entry.

    A drift guard for tests (not runtime): adding or renaming a required entry
    without updating the derived frozensets is caught here.

    Raises:
        AssertionError: If a required class name or alias is missing from the
            corresponding ``*_REQUIRED_NAMES`` set.
    """
    for label, entries, names_set in (
        ("MAIN", _MAIN_REQUIRED, MAIN_REQUIRED_NAMES),
        ("SUBAGENT", _SUBAGENT_REQUIRED, SUBAGENT_REQUIRED_NAMES),
    ):
        for entry in entries:
            expected = {entry.cls.__name__, *entry.names}
            uncovered = expected - names_set
            if uncovered:
                raise AssertionError(
                    f"{label}_REQUIRED_NAMES drift: {uncovered} not covered. "
                    f"Update the derived frozenset after changing _REQUIRED."
                )
