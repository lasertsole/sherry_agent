"""The single declarative middleware specification table (audit M10 / M11 / M13).

Every middleware is declared EXACTLY ONCE here: its class, one binding per
agent profile (main / subagent child) carrying that profile's registration
position and constructor, whether the profile REQUIRES it (the scaffolding
safety baseline), and — where the profiles differ — the replacement class the
child profile constructs (``TokenBudget`` → ``ChildTokenBudget``).

Both chains are derived from this table:

* ``agent/core.py::_build_middlewares`` → :func:`build_main_middlewares`
* ``agent/tools/subagent/spawn/child_agent_factory.py::_build_child_middlewares``
  → :func:`build_child_middlewares`

``agent/middlewares/scaffolding.py`` derives ``_MAIN_REQUIRED`` /
``_SUBAGENT_REQUIRED`` from the same rows, and ``agent/middlewares/catalog.py``
exports :func:`main_middleware_order`. Adding or moving a middleware is one
table edit plus (for a safety-critical entry) its ``required_in`` flag — never
four scattered lists.

ORDER IS A CONTRACT: LangChain runs ``after_model`` nodes in REVERSE
registration order and ``wrap_model_call`` layers outermost-first, so each
binding's ``position`` is load-bearing. The positions are validated at import
time (unique and contiguous per profile), and the order-pinning tests in
``tests/agent/core/test_middleware_order.py`` still assert the literal
sequence.

The table is intentionally NOT derived from the builders: constructing a chain
needs runtime LLM instances and feature flags. The builders here are plain
callables resolved at build time, which also keeps the test seam intact —
monkeypatching a class name in this module is observed by the next build.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from config.features import DELEGATION_PRESSURE, ITERATION_BUDGET

from .context_eviction import ContextEvictionMiddleware
from .delegation_pressure import DelegationPressureGate
from .heartbeat_staleness import HeartbeatStaleness
from .humanInTheLoop import HITLConfig, HumanInTheLoop
from .invalid_tool_call_repair import InvalidToolCallRepairMiddleware
from .iteration_budget import IterationBudget
from .llm_retry import LLMRetryMiddleware
from .max_tokens_boost import MaxTokensBoostMiddleware
from .media_pipeline import MultimodalProcessor
from .message_persistence import MessagePersistenceMiddleware
from .output_repetition_guard import OutputRepetitionGuard
from .path_guard import PathGuard
from .plan_mode_gate import PlanModeToolGate
from .reminder_catalog import DynamicReminderMiddleware
from .safety_filter import InputSafetyFilter, LLMModerationMiddleware, OutputSafetyFilter
from .subagent_completion_drain import SubagentCompletionDrainMiddleware
from .summarization import Summarization
from .summarization.thresholds import resolve_compression_threshold
from .system_prompt import system_prompt_injection
from .task_intent import TaskIntentMiddleware
from .thinking_control import ThinkingControlMiddleware
from .todo_continuation import PlanContinuationEnforcer
from .token_budget import ChildTokenBudget, TokenBudget
from .tool_call_normalize import ToolCallNormalize
from .tool_guardrails import ToolGuardrails
from .tool_resource_lock import ToolResourceLockMiddleware
from .tool_result_enrichment import ToolResultEnrichmentMiddleware
from .user_hooks import UserHooksMiddleware
from .workspace_notice import WorkspaceNoticeMiddleware

__all__ = [
    "CHILD",
    "CHILD_CHAIN",
    "MAIN",
    "MAIN_CHAIN",
    "ChildBuildContext",
    "MainBuildContext",
    "MiddlewareSpec",
    "ProfileBinding",
    "RequiredMiddlewareEntry",
    "build_child_middlewares",
    "build_main_middlewares",
    "chain_for",
    "main_middleware_order",
    "required_entries",
]

MAIN: Literal["main"] = "main"
CHILD: Literal["child"] = "child"
Profile = Literal["main", "child"]


@dataclass(frozen=True)
class MainBuildContext:
    """Runtime dependencies of the main-agent chain.

    Mirrors the keyword signature of ``agent/core.py::_build_middlewares`` so
    the thin wrapper there can pass its arguments through unchanged.
    """

    fallback_chain: Any
    auxiliary_llm: Any
    main_llm_context_window: int
    compression_trigger_ratio: float
    compression_llm: Any = None
    temperature: float | None = None


@dataclass(frozen=True)
class ChildBuildContext:
    """Pre-resolved inputs of the subagent child chain.

    The depth-scaled summary budget and the compression threshold are computed
    by the child factory (they read the spawn depth / main output budget) and
    handed in, so this module never imports the subagent package.
    """

    auxiliary_llm: Any
    main_llm_context_window: int
    iteration_limit: int
    summary_total_max_tokens: int
    compression_threshold: int


@dataclass(frozen=True)
class ProfileBinding:
    """How one middleware is registered in one agent profile.

    Attributes:
        position: Zero-based registration index within the profile's chain.
        build: Constructor called with the profile's build context; resolved
            from this module's globals at build time (keeps the test seam
            working when a class is monkeypatched).
        when: Optional predicate for a conditionally registered entry
            (``DelegationPressureGate``).
    """

    position: int
    build: Callable[[Any], Any]
    when: Callable[[], bool] | None = None


class RequiredMiddlewareEntry(NamedTuple):
    """A required middleware entry — the class plus accepted string aliases.

    Mirrors deepagents' ``(class, aliases)`` tuple pattern: a required entry is
    satisfied by an exact class match OR by any accepted name (``cls.__name__``
    plus the optional aliases in ``names``).
    """

    cls: type
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class MiddlewareSpec:
    """One row of the middleware table.

    Attributes:
        name: Display/key name (the class name for every constructor entry;
            ``system_prompt_injection`` keeps its function name).
        cls: The main-profile class — also the required-set identity.
        main: Main-profile binding, or ``None`` when main does not carry it.
        child: Child-profile binding, or ``None`` when children do not.
        child_cls: Replacement class for the child profile when it differs from
            ``cls`` (``TokenBudget`` → ``ChildTokenBudget``).
        required_in: Profiles whose scaffolding baseline includes this entry.
    """

    name: str
    cls: type
    main: ProfileBinding | None = None
    child: ProfileBinding | None = None
    child_cls: type | None = None
    required_in: frozenset[Profile] = frozenset()

    def binding(self, profile: Profile) -> ProfileBinding | None:
        """The binding for *profile*, or ``None`` when it is not a member."""
        return self.main if profile == MAIN else self.child

    def class_for(self, profile: Profile) -> type:
        """The class required-set validation should look for in *profile*."""
        if profile == CHILD and self.child_cls is not None:
            return self.child_cls
        return self.cls


# ---------------------------------------------------------------------------
# Builders with profile-specific parameters
# ---------------------------------------------------------------------------


def _main_max_output() -> int | None:
    """The configured main output cap, resolved lazily (model import is heavy)."""
    from models.LLMs.main_llm import get_max_output_tokens

    return get_max_output_tokens()


def _build_main_summarization(ctx: MainBuildContext) -> Summarization:
    return Summarization(
        need_update_system_prompt=True,
        # COMPRESSION_LLM_* when configured, else the auxiliary instance
        # (the fallback lives in the factory; a bare build can still pass None
        # and keep the old wiring).
        model=ctx.compression_llm or ctx.auxiliary_llm,
        main_llm_context_window=ctx.main_llm_context_window,
        trigger=[
            (
                "tokens",
                resolve_compression_threshold(
                    ctx.main_llm_context_window,
                    _main_max_output(),
                    fallback_ratio=ctx.compression_trigger_ratio,
                )[0],
            )
        ],
    )


def _build_child_summarization(ctx: ChildBuildContext) -> Summarization:
    return Summarization(
        model=ctx.auxiliary_llm,
        main_llm_context_window=ctx.main_llm_context_window,
        # A subagent's summary is consumed by every ancestor: shallow layers
        # carry more history and get a larger depth-scaled budget.
        summary_total_max_tokens=ctx.summary_total_max_tokens,
        trigger=[
            ("messages", 40),
            ("tokens", ctx.compression_threshold),
        ],
    )


def _build_main_iteration_budget(_ctx: MainBuildContext) -> IterationBudget:
    return IterationBudget(ITERATION_BUDGET["main_agent_max_iterations"])


def _build_child_iteration_budget(ctx: ChildBuildContext) -> IterationBudget:
    return IterationBudget(ctx.iteration_limit)


def _build_main_thinking_control(ctx: MainBuildContext) -> ThinkingControlMiddleware:
    return ThinkingControlMiddleware(temperature=ctx.temperature)


def _build_main_llm_retry(ctx: MainBuildContext) -> LLMRetryMiddleware:
    return LLMRetryMiddleware(fallback_chain=ctx.fallback_chain)


def _delegation_pressure_enabled() -> bool:
    return os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1" or bool(
        DELEGATION_PRESSURE["enabled"]
    )


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

#: Required profiles for the shared safety baseline.
_BOTH: frozenset[Profile] = frozenset({MAIN, CHILD})

#: Single source of truth: one row per middleware, in main-chain order.
MIDDLEWARE_SPECS: tuple[MiddlewareSpec, ...] = (
    # Todo-continuation: registered FIRST so its after_agent hook runs LAST —
    # after_agent hooks execute in REVERSE list order, so the first registered
    # middleware sits closest to END. It must observe the truly finished turn.
    MiddlewareSpec(
        name="PlanContinuationEnforcer",
        cls=PlanContinuationEnforcer,
        main=ProfileBinding(0, lambda _ctx: PlanContinuationEnforcer()),
    ),
    # @dynamic_prompt middleware INSTANCE (not a constructor): the outermost
    # wrap_model_call layer in this list.
    MiddlewareSpec(
        name="system_prompt_injection",
        cls=type(system_prompt_injection),
        main=ProfileBinding(1, lambda _ctx: system_prompt_injection),
    ),
    # Working-directory change notice: a before_agent node, so it runs once per
    # turn and BEFORE every before_model hook.
    MiddlewareSpec(
        name="WorkspaceNoticeMiddleware",
        cls=WorkspaceNoticeMiddleware,
        main=ProfileBinding(2, lambda _ctx: WorkspaceNoticeMiddleware()),
        required_in=frozenset({MAIN}),
    ),
    # Plan mode's read-only tool whitelist.
    MiddlewareSpec(
        name="PlanModeToolGate",
        cls=PlanModeToolGate,
        main=ProfileBinding(3, lambda _ctx: PlanModeToolGate()),
    ),
    MiddlewareSpec(
        name="MultimodalProcessor",
        cls=MultimodalProcessor,
        main=ProfileBinding(4, lambda _ctx: MultimodalProcessor()),
        required_in=frozenset({MAIN}),
    ),
    MiddlewareSpec(
        name="IterationBudget",
        cls=IterationBudget,
        main=ProfileBinding(5, _build_main_iteration_budget),
        # The child takes the same class with the role-dependent worker/executor
        # limit resolved by the factory.
        child=ProfileBinding(1, _build_child_iteration_budget),
        required_in=_BOTH,
    ),
    # Session-level token cap (optional; absent = pass-through) and usage
    # accumulation. The child profile replaces it with the attribution-only
    # ChildTokenBudget (it reports usage to the root parent instead of capping).
    MiddlewareSpec(
        name="TokenBudget",
        cls=TokenBudget,
        main=ProfileBinding(6, lambda _ctx: TokenBudget()),
        child=ProfileBinding(2, lambda _ctx: ChildTokenBudget()),
        child_cls=ChildTokenBudget,
    ),
    # Outermost wrap_tool_call: append the injector queue's output to the final
    # ToolMessage.
    MiddlewareSpec(
        name="ToolResultEnrichmentMiddleware",
        cls=ToolResultEnrichmentMiddleware,
        main=ProfileBinding(7, lambda _ctx: ToolResultEnrichmentMiddleware()),
    ),
    MiddlewareSpec(
        name="ToolGuardrails",
        cls=ToolGuardrails,
        main=ProfileBinding(8, lambda _ctx: ToolGuardrails()),
        child=ProfileBinding(3, lambda _ctx: ToolGuardrails()),
        required_in=_BOTH,
    ),
    # Process-wide shell/code and per-session HITL resource locks. Registered
    # after ToolGuardrails (inner to it), so a blocked call never waits on a
    # lock it will not execute.
    MiddlewareSpec(
        name="ToolResourceLockMiddleware",
        cls=ToolResourceLockMiddleware,
        main=ProfileBinding(9, lambda _ctx: ToolResourceLockMiddleware()),
        child=ProfileBinding(4, lambda _ctx: ToolResourceLockMiddleware()),
        required_in=_BOTH,
    ),
    # User-configurable lifecycle hooks (sherry.jsonc ``hooks``).
    MiddlewareSpec(
        name="UserHooksMiddleware",
        cls=UserHooksMiddleware,
        main=ProfileBinding(10, lambda _ctx: UserHooksMiddleware()),
    ),
    # Registered directly after ToolGuardrails, i.e. OUTER relative to PathGuard
    # / HITL / MessagePersistenceMiddleware in the wrap chain.
    MiddlewareSpec(
        name="ContextEvictionMiddleware",
        cls=ContextEvictionMiddleware,
        main=ProfileBinding(11, lambda _ctx: ContextEvictionMiddleware()),
        required_in=frozenset({MAIN}),
    ),
    MiddlewareSpec(
        name="ToolCallNormalize",
        cls=ToolCallNormalize,
        main=ProfileBinding(12, lambda _ctx: ToolCallNormalize()),
        child=ProfileBinding(7, lambda _ctx: ToolCallNormalize()),
        required_in=_BOTH,
    ),
    MiddlewareSpec(
        name="PathGuard",
        cls=PathGuard,
        main=ProfileBinding(13, lambda _ctx: PathGuard()),
        required_in=frozenset({MAIN}),
    ),
    MiddlewareSpec(
        name="SubagentCompletionDrainMiddleware",
        cls=SubagentCompletionDrainMiddleware,
        main=ProfileBinding(14, lambda _ctx: SubagentCompletionDrainMiddleware()),
    ),
    MiddlewareSpec(
        name="TaskIntentMiddleware",
        cls=TaskIntentMiddleware,
        main=ProfileBinding(15, lambda _ctx: TaskIntentMiddleware()),
    ),
    # Live-view reminders (<system-reminder> tags), injected from live state
    # once per turn after TaskIntent.
    MiddlewareSpec(
        name="DynamicReminderMiddleware",
        cls=DynamicReminderMiddleware,
        main=ProfileBinding(16, lambda _ctx: DynamicReminderMiddleware()),
    ),
    # Live-stream safety (OFF by default). The LLM review sits INSIDE the
    # keyword filter so a keyword block never pays for a review call.
    MiddlewareSpec(
        name="InputSafetyFilter",
        cls=InputSafetyFilter,
        main=ProfileBinding(17, lambda _ctx: InputSafetyFilter()),
    ),
    MiddlewareSpec(
        name="LLMModerationMiddleware",
        cls=LLMModerationMiddleware,
        main=ProfileBinding(18, lambda _ctx: LLMModerationMiddleware()),
    ),
    # Same class in both profiles. Children run via ``ainvoke`` so the guard's
    # non-streaming interception covers their whole model result before it is
    # returned to the caller (state dedupe counters stay per child session).
    MiddlewareSpec(
        name="OutputRepetitionGuard",
        cls=OutputRepetitionGuard,
        main=ProfileBinding(19, lambda _ctx: OutputRepetitionGuard()),
        child=ProfileBinding(5, lambda _ctx: OutputRepetitionGuard()),
        required_in=_BOTH,
    ),
    # Children also run via ``ainvoke``: the non-streaming path means no
    # callback stripping, and a truncated tool call is recovered the same way.
    MiddlewareSpec(
        name="MaxTokensBoostMiddleware",
        cls=MaxTokensBoostMiddleware,
        main=ProfileBinding(20, lambda _ctx: MaxTokensBoostMiddleware()),
        child=ProfileBinding(6, lambda _ctx: MaxTokensBoostMiddleware()),
        required_in=_BOTH,
    ),
    # Per-session thinking toggle: swaps the per-call model for the thinking
    # on/off variant when the client flag is set (inner relative to
    # MaxTokensBoost).
    MiddlewareSpec(
        name="ThinkingControlMiddleware",
        cls=ThinkingControlMiddleware,
        main=ProfileBinding(21, _build_main_thinking_control),
    ),
    MiddlewareSpec(
        name="HeartbeatStaleness",
        cls=HeartbeatStaleness,
        main=ProfileBinding(22, lambda _ctx: HeartbeatStaleness()),
        child=ProfileBinding(8, lambda _ctx: HeartbeatStaleness()),
        required_in=_BOTH,
    ),
    MiddlewareSpec(
        name="HumanInTheLoop",
        cls=HumanInTheLoop,
        main=ProfileBinding(23, lambda _ctx: HumanInTheLoop(HITLConfig())),
        required_in=frozenset({MAIN}),
    ),
    # Registered directly after HITL so its after_model node runs FIRST —
    # LangChain chains after_model nodes in reverse registration order.
    MiddlewareSpec(
        name="MessagePersistenceMiddleware",
        cls=MessagePersistenceMiddleware,
        main=ProfileBinding(24, lambda _ctx: MessagePersistenceMiddleware()),
        required_in=frozenset({MAIN}),
    ),
    # Between HITL and Summarization: INNER relative to MaxTokensBoost and
    # OUTER relative to Summarization.
    MiddlewareSpec(
        name="LLMRetryMiddleware",
        cls=LLMRetryMiddleware,
        main=ProfileBinding(25, _build_main_llm_retry),
        required_in=frozenset({MAIN}),
    ),
    # Output-side keyword rules (OFF by default). Registered AFTER
    # MessagePersistence so its after_model node runs FIRST.
    MiddlewareSpec(
        name="OutputSafetyFilter",
        cls=OutputSafetyFilter,
        main=ProfileBinding(26, lambda _ctx: OutputSafetyFilter()),
    ),
    # The child profile triggers on message count AND the shared compression
    # line; both parameter sets are declared in the two builders above.
    MiddlewareSpec(
        name="Summarization",
        cls=Summarization,
        main=ProfileBinding(27, _build_main_summarization),
        child=ProfileBinding(0, _build_child_summarization),
        required_in=_BOTH,
    ),
    # Registered LAST = the innermost wrap_model_call layer, closest to the raw
    # model call, in BOTH profiles: the repaired message is what routes the
    # agent loop, so state / checkpointer / persistence all see it. A child
    # whose only call failed JSON parsing otherwise ends its invocation on a
    # narration with no edit.
    MiddlewareSpec(
        name="InvalidToolCallRepairMiddleware",
        cls=InvalidToolCallRepairMiddleware,
        main=ProfileBinding(28, lambda _ctx: InvalidToolCallRepairMiddleware()),
        child=ProfileBinding(9, lambda _ctx: InvalidToolCallRepairMiddleware()),
        required_in=_BOTH,
    ),
    # Main-agent-only soft delegation pressure, appended when its feature flag
    # (or the eval override) is on. Children never receive it.
    MiddlewareSpec(
        name="DelegationPressureGate",
        cls=DelegationPressureGate,
        main=ProfileBinding(
            29, lambda _ctx: DelegationPressureGate(), _delegation_pressure_enabled
        ),
    ),
)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _binding_or_raise(spec: MiddlewareSpec, profile: Profile) -> ProfileBinding:
    binding = spec.binding(profile)
    if binding is None:  # pragma: no cover - guarded by _validate_table()
        raise RuntimeError(f"{spec.name} has no {profile} binding")
    return binding


def chain_for(profile: Profile) -> tuple[MiddlewareSpec, ...]:
    """The declared chain for *profile*, ordered by binding position."""
    members = [spec for spec in MIDDLEWARE_SPECS if spec.binding(profile) is not None]
    members.sort(key=lambda spec: _binding_or_raise(spec, profile).position)
    return tuple(members)


MAIN_CHAIN: tuple[MiddlewareSpec, ...] = chain_for(MAIN)
CHILD_CHAIN: tuple[MiddlewareSpec, ...] = chain_for(CHILD)


def _validate_table() -> None:
    """Fail at import time on a malformed table (drift guard, not runtime check)."""
    names = [spec.name for spec in MIDDLEWARE_SPECS]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise RuntimeError(f"Duplicate middleware spec names: {duplicates}")

    for profile, chain in ((MAIN, MAIN_CHAIN), (CHILD, CHILD_CHAIN)):
        positions = [_binding_or_raise(spec, profile).position for spec in chain]
        if positions != list(range(len(chain))):
            raise RuntimeError(
                f"{profile} profile positions must be unique and contiguous "
                f"0..{len(chain) - 1}; got {positions}"
            )
        for spec in MIDDLEWARE_SPECS:
            if profile in spec.required_in and spec.binding(profile) is None:
                raise RuntimeError(
                    f"{spec.name} is required in the {profile} profile but has no {profile} binding"
                )


_validate_table()


def _build_chain(profile: Profile, context: Any) -> list[Any]:
    chain: list[Any] = []
    members = MAIN_CHAIN if profile == MAIN else CHILD_CHAIN
    for spec in members:
        binding = _binding_or_raise(spec, profile)
        if binding.when is not None and not binding.when():
            continue
        chain.append(binding.build(context))
    return chain


def build_main_middlewares(context: MainBuildContext) -> list[Any]:
    """Instantiate the main-agent chain derived from :data:`MIDDLEWARE_SPECS`."""
    return _build_chain(MAIN, context)


def build_child_middlewares(context: ChildBuildContext) -> list[Any]:
    """Instantiate the subagent child chain derived from :data:`MIDDLEWARE_SPECS`."""
    return _build_chain(CHILD, context)


def main_middleware_order() -> tuple[str, ...]:
    """Declared main-chain names in registration order (conditional entries included)."""
    return tuple(spec.name for spec in MAIN_CHAIN)


def required_entries(profile: Profile) -> tuple[RequiredMiddlewareEntry, ...]:
    """The scaffolding baseline for *profile*, derived from the table.

    Entries follow the profile's own registration order, so the baseline reads
    as a filtered view of the chain it protects.
    """
    members = MAIN_CHAIN if profile == MAIN else CHILD_CHAIN
    return tuple(
        RequiredMiddlewareEntry(spec.class_for(profile), ())
        for spec in members
        if profile in spec.required_in
    )
