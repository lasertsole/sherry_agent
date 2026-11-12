"""Construction of the child agent: tool face, LLM selection, middleware, config.

Everything that turns a validated spawn into a runnable LangGraph agent lives
here — the depth-scaled budgets, the role-driven tool families, the model
precedence (profile → override → functional-role tier → depth role), the
structured-output degradation ladder's effect on the build, and the child's
session-level context binding.
"""

import os
from typing import Any

from loguru import logger

from ..config import get_config
from ..types.capability import SubagentSessionRole
from ..types.functional_role import CODE_INTEL_ROLES, PTC_ROLES, FunctionalRole
from ..types.registry import SubagentRunRecord
from ..output_caps import resolve_profile_max_output_tokens
from ..registry.session_keys import normalize_session_key
from .inherited_tool_policy import apply_tool_policy

#: Error text markers of a provider rejecting the structured-output strategy
#: (a forced ``tool_choice``, or JSON mode it cannot combine with thinking).
#: Only schema-bound children consult this, so a generic "thinking mode" 400
#: from some other call never degrades a model.
_STRUCTURED_OUTPUT_REJECTION_MARKERS = (
    "does not support this tool_choice",
    "thinking mode",
    "response_format",
    "structured output",
)

#: Transient provider/transport failures must never degrade a model: a 429 or a
#: reset connection that happens to mention ``response_format`` is not a
#: capability rejection, and persisting a degradation for it costs every later
#: spawn.
_TRANSIENT_EXCLUSION_MARKERS = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "timeout",
    "timed out",
    "connection",
    "temporarily",
)


def _is_structured_output_rejection(exc: BaseException) -> bool:
    """True when ``exc`` looks like a provider rejecting the JSON strategy."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _TRANSIENT_EXCLUSION_MARKERS):
        return False
    return any(marker in text for marker in _STRUCTURED_OUTPUT_REJECTION_MARKERS)


def _effective_child_deny(run: SubagentRunRecord) -> list[str]:
    """The child's deny-list: inherited policy + every tool whose scope is absent."""
    effective_deny = list(run.inherited_tool_deny)
    if run.scopes:
        scope_tool_map = {
            "subagent:spawn": "sessions_spawn",
            "subagent:kill": "sessions_kill",
            "subagent:yield": "sessions_yield",
            "subagent:send": "sessions_send",
        }
        for scope, tool_name in scope_tool_map.items():
            # If the scope is missing from the run's scopes, deny the corresponding tool
            if scope not in run.scopes and tool_name not in effective_deny:
                effective_deny.append(tool_name)
    return effective_deny


def _bind_child_run_context(run: SubagentRunRecord) -> dict:
    """Build the child's LangGraph config and bind its session-level context.

    Sets the thinking tag, pins the child's project directory (the register is
    the only channel its tools can read — LangGraph's ``config`` is not
    reachable from a tool body), and stamps the keys the external-path gate
    reads: allowlist inheritance from the parent session plus the subagent
    identification that keeps a child fail-closed without main approval.
    """
    from pub.func import build_agent_config

    agent_config = build_agent_config(session_id=run.child_session_key)
    if run.thinking:
        agent_config["tags"] = agent_config.get("tags", [])
        # LangGraph inspects tags for the "thinking:<level>" pattern
        agent_config["tags"].append(f"thinking:{run.thinking}")
    if run.spawned_cwd:
        # The child's tools resolve against this root. It is persisted as the
        # CHILD session's own project-dir binding (resolved at spawn time,
        # inside the lane slot) because that register is the only channel the
        # tools can read — LangGraph's ``config`` is not reachable from a
        # tool body (no ToolRuntime). Freezing it here is what makes a child
        # independent: a later switch in the parent session does not follow.
        from runtime.session import project_dir as project_dir_mod

        project_dir_mod.write_project_dir(run.child_session_key, run.spawned_cwd)

    # Security: the child's file tools resolve external-path authorization
    # against these keys — allowlist inheritance (parent session) and
    # subagent identification (fail-closed without main-session approval).
    from runtime import StateKey, state_register_mem

    state_register_mem.set_state(
        run.child_session_key,
        StateKey.REQUESTER_SESSION_KEY,
        normalize_session_key(run.spawned_by),
    )
    state_register_mem.set_state(run.child_session_key, StateKey.CALLER_SCOPE, "subagent")

    # Token attribution: a child's model calls belong to the ROOT parent's
    # session and the turn that spawned its tree. A direct child takes the root's
    # current turn; every deeper level INHERITS its parent's attribution, so one
    # spawn tree always lands in one turn and a grandchild never re-roots at its
    # immediate parent.
    parent_key = normalize_session_key(run.requester_session_key)
    if parent_key:
        parent_attr = state_register_mem.get_state(parent_key, StateKey.TOKEN_BUDGET_PARENT, None)
        if isinstance(parent_attr, dict) and isinstance(parent_attr.get("session_id"), str):
            root = parent_attr["session_id"]
            turn_id = parent_attr.get("turn_id")
        else:
            root = parent_key
            turn_id = state_register_mem.get_state(root, StateKey.TOKEN_BUDGET_TURN_ID, None)
        if isinstance(root, str) and root:
            # Write the attribution even without a turn id: the child still
            # counts toward the root's SESSION usage, and ChildTokenBudget adds
            # it to the current turn only when this id matches.
            state_register_mem.set_state(
                run.child_session_key,
                StateKey.TOKEN_BUDGET_PARENT,
                {
                    "session_id": root,
                    "turn_id": turn_id if isinstance(turn_id, str) else "",
                },
            )
    return agent_config


def _effective_child_summary_tokens(spawn_depth: int) -> int:
    """The depth-scaled SummaryDoc budget for a subagent (main is unaffected)."""
    from config.features.agent_side.summarization import effective_summary_max_tokens

    return effective_summary_max_tokens(spawn_depth, get_config().max_spawn_depth)


def _compression_threshold_for_children(context_window: int) -> int:
    """The main-model compression line shared by child Summarization triggers."""
    from config.features import SUMMARIZATION
    from models.LLMs.main_llm import get_max_output_tokens

    from agent.middlewares.summarization.thresholds import resolve_compression_threshold

    return resolve_compression_threshold(
        context_window,
        get_max_output_tokens(),
        fallback_ratio=SUMMARIZATION["compression_trigger_ratio"],
    )[0]


def _build_child_middlewares(
    *,
    auxiliary_llm: Any,
    main_llm_context_window: int,
    functional_role: FunctionalRole = FunctionalRole.GENERAL,
    spawn_depth: int = 1,
) -> list[Any]:
    """Assemble the child-agent middleware pipeline.

    ORDER IS A CONTRACT (the subagent side of the middleware-order tests).
    Extracted from :func:`_build_child_agent` so the scaffolding validation and
    its drift-guard test can inspect the exact list handed to ``create_agent``.
    The chain itself is DERIVED from :data:`agent.middlewares.specs.MIDDLEWARE_SPECS`
    (the same table that drives the main chain and both required sets); this
    function only resolves the role-dependent iteration budget and the
    depth-scaled summary budget into a :class:`ChildBuildContext`. The executor
    is the write-capable worker and gets the larger iteration budget; every
    other role keeps the standard worker budget.
    """
    from agent.middlewares.specs import ChildBuildContext, build_child_middlewares
    from config.features import ITERATION_BUDGET

    iteration_limit = (
        ITERATION_BUDGET["executor_max_iterations"]
        if functional_role == FunctionalRole.EXECUTOR
        else ITERATION_BUDGET["worker_max_iterations"]
    )
    logger.info(
        "Subagent middleware budget: functional_role={} max_iterations={}",
        functional_role,
        iteration_limit,
    )

    return build_child_middlewares(
        ChildBuildContext(
            auxiliary_llm=auxiliary_llm,
            main_llm_context_window=main_llm_context_window,
            iteration_limit=iteration_limit,
            summary_total_max_tokens=_effective_child_summary_tokens(spawn_depth),
            compression_threshold=_compression_threshold_for_children(main_llm_context_window),
        )
    )


def _assert_child_token_floor() -> None:
    """Subagent spawn bypasses ``built_agent()``, so validate the 200K floor here.

    Child LLM construction would otherwise inherit the floor implicitly — this
    is the same guard the main agent applies.
    """
    from config.features import (
        LLM_CLIENT_DEFAULTS,
        assert_max_output_valid,
        assert_max_token_valid,
    )

    _main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
    _aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
    _main_val = int(_main_raw) if _main_raw else None
    _aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
    _main_out_raw = os.getenv("MAIN_LLM_MAX_OUTPUT_TOKEN", "").strip()
    _aux_out_raw = os.getenv("AUXILIARY_LLM_MAX_OUTPUT_TOKEN", "").strip()
    assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
    assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
    assert_max_output_valid(
        "MAIN_LLM_MAX_OUTPUT_TOKEN", int(_main_out_raw) if _main_out_raw else None
    )
    assert_max_output_valid(
        "AUXILIARY_LLM_MAX_OUTPUT_TOKEN", int(_aux_out_raw) if _aux_out_raw else None
    )


def _assemble_child_tools(
    tools: list | None,
    tool_allow: list[str] | None,
    tool_deny: list[str] | None,
    extra_tools: list[str] | None,
    functional_role: FunctionalRole,
    session_id: str,
) -> list:
    """The child's tool face: role policy, then the role-gated families.

    Code intelligence (researcher + librarian) and PTC (executor) are injected
    here and never added to ``_MAIN_TOOLS_BUILDERS``, so the main agent and
    every other functional role cannot see them; ast-grep is available to every
    functional role but also stays out of the main agent's face.
    """
    from agent.tools import build_main_tools

    base_tools = tools if tools is not None else build_main_tools()

    # Per-task extra tools attach on top of the role policy: they join the
    # allow-list only when a whitelist is active (in inherit mode they are
    # already part of the candidate set and only the deny-list applies). The
    # main_only metadata gate and deny-list still apply to them.
    effective_allow = list(tool_allow or [])
    if extra_tools and effective_allow:
        for name in extra_tools:
            if name not in effective_allow:
                effective_allow.append(name)

    filtered_tools = apply_tool_policy(base_tools, effective_allow, tool_deny)

    # Code intelligence tools are CODE_INTEL_ROLES-only (researcher, librarian
    # and the write roles executor/general): they are injected here, after the
    # role policy, and never added to _MAIN_TOOLS_BUILDERS, so the main agent
    # and the remaining roles (reviewer) cannot see them. The read-only/write
    # distinction lives in the role prompt, not in the injected tool set.
    final_tools = list(filtered_tools)
    if functional_role in CODE_INTEL_ROLES:
        from agent.tools.code_intel import build_code_intel_tools
        from agent.tools.code_intel.lsp import build_lsp_tools

        final_tools = [
            *final_tools,
            *build_code_intel_tools(session_id=session_id),
            *build_lsp_tools(session_id=session_id),
        ]

    # ast-grep structural search/rewrite is a core component available to EVERY
    # functional role (not RESEARCHER-only), mirroring oh-my-openagent's globally
    # registered ast-grep MCP server. It is never added to _MAIN_TOOLS_BUILDERS,
    # so the main agent cannot see it; the builder is fail-open (a missing
    # binary degrades to an actionable install-hint tool result, never a crash).
    from agent.tools.code_intel.ast_grep import build_ast_grep_tools

    final_tools = [*final_tools, *build_ast_grep_tools(session_id=session_id)]

    # Programmatic Tool Calling is EXECUTOR-only (PTC_ROLES) — the same single
    # named source that spawn/system_prompt.py reads. Injected after the role
    # policy and never added to _MAIN_TOOLS_BUILDERS, so the main agent and
    # every other functional role cannot see it. The tool's own whitelist is
    # intersected with filtered_tools, so it can call only real executor tools
    # and can never recurse into execute_code.
    if functional_role in PTC_ROLES:
        from agent.tools.ptc import build_ptc_tool

        final_tools = [
            *final_tools,
            build_ptc_tool(available_tools=filtered_tools, session_id=session_id),
        ]
    return final_tools


def _select_child_llm(
    model_profile: dict[str, str] | None,
    model_override: str | None,
    functional_role: FunctionalRole,
    model_tier: str | None,
    role: SubagentSessionRole,
    *,
    thinking_off: bool = False,
):
    """Pick the child's LLM, in precedence order.

    *model_profile* (the requester session's per-role model, 预设-子代理模型) wins:
    it is a full profile descriptor, so the child can run on another provider /
    key entirely. A build failure logs and falls through to the next step. Then
    *model_override* (resolved by name), then the functional role's *model_tier*,
    and finally the depth role: ORCHESTRATOR gets the main LLM, LEAF the
    auxiliary one.

    ``thinking_off`` forces the main-family builders to the thinking-off variant.
    The structured-output degradation ladder sets it only at its LAST level: a
    child whose forced ``tool_choice`` the provider rejects gets there after
    the self-sent ``response_format`` level also failed (see
    :mod:`agent.tools.subagent.model_caps`).

    The auxiliary family has no thinking knob by default; ``thinking`` is
    forwarded only when ``thinking_off`` is set.
    """
    if thinking_off:
        logger.info(
            "Subagent structured output: thinking forced off (functional_role={}, tier={})",
            functional_role,
            model_tier,
        )

    for resolver in _MODEL_RESOLVERS:
        llm = resolver.resolve(
            model_profile=model_profile,
            model_override=model_override,
            functional_role=functional_role,
            model_tier=model_tier,
            role=role,
            thinking_off=thinking_off,
        )
        if llm is not None:
            return llm
    # The role resolver always answers; this is a defensive fallback.
    return _aux_child_llm(thinking_off)


def _main_child_llm(thinking_off: bool):
    from models import build_main_llm

    return build_main_llm(thinking=False) if thinking_off else build_main_llm()


def _aux_child_llm(thinking_off: bool):
    from models import build_auxiliary_llm

    return build_auxiliary_llm(thinking=False) if thinking_off else build_auxiliary_llm()


class _ModelResolver:
    """One rung of the child-LLM precedence chain (audit L4)."""

    def resolve(
        self,
        *,
        model_profile: dict[str, str] | None,
        model_override: str | None,
        functional_role: FunctionalRole,
        model_tier: str | None,
        role: SubagentSessionRole,
        thinking_off: bool,
    ):
        raise NotImplementedError


class _ProfileResolver(_ModelResolver):
    def resolve(self, **kwargs):
        model_profile = kwargs["model_profile"]
        thinking_off = kwargs["thinking_off"]
        functional_role = kwargs["functional_role"]
        if not (model_profile and model_profile.get("model")):
            return None
        try:
            from models import build_main_llm_for_profile

            profile_kwargs: dict[str, Any] = {
                "provider": model_profile.get("provider"),
                "model": model_profile["model"],
                "api_key": model_profile.get("api_key"),
                "base_url": model_profile.get("base_url"),
                "max_output_tokens": resolve_profile_max_output_tokens(model_profile),
            }
            if thinking_off:
                # Keep the free-form call shape untouched; only a schema-bound
                # child pins the variant.
                profile_kwargs["thinking"] = False
            child_llm = build_main_llm_for_profile(**profile_kwargs)
            logger.info(
                "Subagent role model: functional_role={} model={} provider={}",
                functional_role,
                model_profile["model"],
                model_profile.get("provider"),
            )
            return child_llm
        except Exception as exc:  # noqa: BLE001 - a bad profile must not kill the spawn
            logger.warning(
                "Subagent role model profile failed ({}); falling back to the role tier", exc
            )
            return None


class _OverrideResolver(_ModelResolver):
    def resolve(self, **kwargs):
        model_override = kwargs["model_override"]
        if not model_override:
            return None
        try:
            from models import build_llm_by_name

            return build_llm_by_name(model_override)
        except (ImportError, AttributeError):
            return None


class _TierResolver(_ModelResolver):
    def resolve(self, **kwargs):
        functional_role = kwargs["functional_role"]
        model_tier = kwargs["model_tier"]
        thinking_off = kwargs["thinking_off"]
        # Functional-role tier wins over the depth role; GENERAL (no tier) keeps
        # the pre-migration depth-based behavior untouched.
        if functional_role == FunctionalRole.GENERAL:
            return None
        if model_tier == "main":
            return _main_child_llm(thinking_off)
        if model_tier == "auxiliary":
            return _aux_child_llm(thinking_off)
        return None


class _RoleResolver(_ModelResolver):
    def resolve(self, **kwargs):
        thinking_off = kwargs["thinking_off"]
        if kwargs["role"] == SubagentSessionRole.ORCHESTRATOR:
            return _main_child_llm(thinking_off)
        return _aux_child_llm(thinking_off)


_MODEL_RESOLVERS: tuple[_ModelResolver, ...] = (
    _ProfileResolver(),
    _OverrideResolver(),
    _TierResolver(),
    _RoleResolver(),
)


def _child_model_identity(
    model_profile: dict[str, str] | None,
    functional_role: FunctionalRole,
    model_tier: str | None,
    role: SubagentSessionRole,
) -> tuple[str, str]:
    """The ``(provider, model)`` :func:`_select_child_llm` is about to build.

    Mirrors that function's PRECEDENCE without building a client: the persisted
    structured-output level is looked up before the model exists. A profile
    whose build later fails falls back to the role tier inside the builder; the
    label then names the profile's model, which is harmless — a failed build is
    not a structured-output rejection, so nothing is recorded for it.
    """
    import os

    from models.LLMs.main_llm import api_name as main_model, model_provider as main_provider

    if model_profile and model_profile.get("model"):
        return (
            str(model_profile.get("provider") or main_provider or ""),
            str(model_profile["model"]),
        )
    main_identity = (str(main_provider or ""), str(main_model or ""))
    aux_identity = (
        os.getenv("AUXILIARY_LLM_PROVIDER") or "",
        os.getenv("AUXILIARY_LLM_API_NAME") or "",
    )
    if functional_role != FunctionalRole.GENERAL and model_tier == "main":
        return main_identity
    if functional_role != FunctionalRole.GENERAL and model_tier == "auxiliary":
        return aux_identity
    if role == SubagentSessionRole.ORCHESTRATOR:
        return main_identity
    return aux_identity


async def _build_child_agent(
    system_prompt: str,
    tools: list | None,
    tool_allow: list[str] | None,
    tool_deny: list[str] | None,
    role: SubagentSessionRole = SubagentSessionRole.LEAF,
    functional_role: FunctionalRole = FunctionalRole.GENERAL,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    session_id: str = "",
    output_schema: dict | None = None,
    strategy_level: int = 0,
    spawn_depth: int = 1,
):
    """Construct a LangGraph agent for the child sub-agent with filtered tools and role-appropriate LLM.

    ``strategy_level`` is the structured-output degradation rung (see
    :mod:`agent.tools.subagent.model_caps`): 0 hands the schema to LangChain's
    ``response_format`` (a forced structured-output tool), 1 sends
    ``response_format={"type": "json_object"}`` ourselves (the schema stays in
    the system prompt and the result is validated locally), 2 does the same with
    thinking OFF. The rung is resolved to a ``StructuredOutputStrategy`` here, so
    the build reads flags rather than comparing the integer. Ignored when no
    ``output_schema`` is given.

    The LLM selection logic is:
      - *model_profile* (the requester session's per-role model, 预设-子代理模型) wins:
        it is a full profile descriptor, so the child can run on another provider /
        key entirely. A build failure logs and falls through to the tier.
      - If *model_override* is provided, attempt to resolve it by name; on failure fall through.
      - A non-GENERAL *functional_role* with *model_tier* "main"/"auxiliary" picks that LLM.
      - Otherwise the depth role decides: ORCHESTRATOR gets the main LLM, LEAF the auxiliary one.

    Middleware stack (applied in order):
      - **Summarization** — condenses conversation when tokens/messages exceed threshold.
      - **IterationBudget(60)** — caps the agent to 60 reasoning/tool-call iterations.
      - **ToolGuardrails** — validates tool calls before dispatch.
      - **OutputRepetitionGuard** — stops the agent from emitting the same text endlessly.
      - **MaxTokensBoostMiddleware** — re-calls the model with a boosted ``max_tokens``
        (base × 2^n, capped) when a truncated response carries cut-off tool calls.
      - **ToolCallNormalize** — normalises tool call format across LLM providers.
      - **HeartbeatStaleness** — detects and recovers from stalled agent loops.

    The returned agent is additionally wrapped in
    :class:`RepetitionGuardWrapper` (``phantom_stream_guard=True``),
    mirroring the main agent in ``agent/core.py``.

    Args:
        system_prompt: System prompt for the child agent.
        output_schema: Optional JSON Schema constraining the child's FINAL
            output. It is enforced at generation time through LangChain's
            ``response_format`` (a raw schema resolves to ``AutoStrategy``: the
            provider's native structured output where supported, else a
            structured-output tool call WITH a forced ``tool_choice``), while
            ``build_structured_output_prompt`` keeps the schema in the system
            prompt as the prompt-level fallback and Tier 1 re-validates on
            resume. Because some providers (DeepSeek) reject a forced
            ``tool_choice`` in thinking mode, a schema-bound child is built with
            thinking OFF (``_select_child_llm(thinking_off=True)``).
            ``None`` = free-form output.
        tools: Tool list; ``None`` falls back to :func:`build_main_tools`.
        tool_allow: Explicit allow-list (empty = no restriction beyond deny-list).
        tool_deny: Explicit deny-list (an empty list is authoritative — it means the
            caller intentionally unblocked the defaults, e.g. ORCHESTRATOR spawn/yield;
            main_only metadata tools are dropped regardless).
        role: :class:`SubagentSessionRole` — depth role, the fallback LLM selector.
        functional_role: :class:`FunctionalRole` — overrides the depth role's LLM
            when it carries a concrete ``model_tier``.
        model_override: Optional model name string to override the default LLM.
        model_tier: Resolved functional-role LLM tier ("main" | "auxiliary").
        extra_tools: Per-spawn tool names attached on top of the role allow-list.
        session_id: Child session key; labels the RESEARCHER-only code-intel tools.

    Returns:
        A fully-constructed LangGraph agent ready for ``ainvoke``.
    """
    from agent.core import StateSchema
    from langchain.agents import create_agent
    from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
    from agent.checkpointer import build_async_sqlite_checkpointer
    from agent.middlewares import _SUBAGENT_REQUIRED, validate_required_middleware
    from .. import model_caps

    _assert_child_token_floor()
    final_tools = _assemble_child_tools(
        tools, tool_allow, tool_deny, extra_tools, functional_role, session_id
    )
    strategy = model_caps.strategy_for(strategy_level)
    self_structured = output_schema is not None and strategy.sends_json_mode
    child_llm = _select_child_llm(
        model_profile,
        model_override,
        functional_role,
        model_tier,
        role,
        # The thinking-off rung exists because the provider rejected even the
        # self-sent response_format while reasoning.
        thinking_off=bool(output_schema) and strategy.thinking_off,
    )
    # Levels 1/2: no framework-level structured output (no forced tool_choice);
    # the provider's JSON mode + the prompt schema + our validator replace it.
    # ``with_request_options`` is used instead of ``bind`` because the graph's
    # ``bind_tools`` returns the INNER model and would drop a plain binding;
    # the option must ride on every actual request.
    agent_llm = child_llm
    if self_structured:
        from models.LLMs.reasoning_normalizer import NormalizingChatModel

        if isinstance(child_llm, NormalizingChatModel):
            agent_llm = child_llm.with_request_options({"response_format": {"type": "json_object"}})
        else:
            agent_llm = child_llm.bind(response_format={"type": "json_object"})

    child_checkpointer = await build_async_sqlite_checkpointer()
    await child_checkpointer.setup()

    child_middleware = _build_child_middlewares(
        auxiliary_llm=child_llm,
        main_llm_context_window=main_llm_max_tokens,
        functional_role=functional_role,
        spawn_depth=spawn_depth,
    )
    # Fail fast BEFORE create_agent(): a silently dropped safety-critical child
    # middleware must abort the spawn, not ship a weakened child.
    validate_required_middleware(child_middleware, chain="subagent", entries=_SUBAGENT_REQUIRED)
    child_agent = create_agent(
        model=agent_llm,
        system_prompt=system_prompt,
        state_schema=StateSchema,
        checkpointer=child_checkpointer,
        tools=final_tools,
        middleware=child_middleware,
        # Level 0: structured output as a CONSTRAINT, not a request — a raw
        # schema makes LangChain auto-pick the provider's native structured
        # output (or a structured-output tool where the provider lacks one).
        # Levels 1/2 already carry the shape themselves (see above).
        response_format=output_schema if (output_schema and not self_structured) else None,
    )

    # Wrap with RepetitionGuardWrapper, mirroring the main agent
    # (``agent/core.py``). The wrapper adds stream-level interception
    # (``astream``) plus an ``ainvoke`` post-hoc backstop on the final
    # AIMessage. ``phantom_stream_guard=True`` is safe here for the same
    # reason as the main agent: this middleware-equipped graph always
    # emits before_agent "updates" (e.g. OutputRepetitionGuard.abefore_agent)
    # before any model text on fresh dict-input runs.
    # The ``OutputRepetitionGuard`` middleware above is intentionally KEPT:
    # children run via ``child_agent.ainvoke(...)``, where the wrapper only
    # inspects the final message, so per-call interception (breaking
    # repetitive tool-call loops mid-flight) still depends on the middleware.
    # The shared ``_INTERNAL_WARNED_KEY`` dedupe gate prevents double
    # warnings, and the wrapper's per-turn state reset bounds any
    # cross-call history duplication.
    from agent.wrapper.repetition_guard import RepetitionGuardWrapper

    child_agent = RepetitionGuardWrapper(child_agent, phantom_stream_guard=True)

    return child_agent
