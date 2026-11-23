"""Run-plan resolution for sub-agents — timeout, model selection, and thinking level."""

import time

from ..config import get_config

#: The depth ladder's step: a run one level above the LEAF gets 3× the leaf
#: budget. Leaves (depth == ``max_spawn_depth``) get the configured default
#: (``run_timeout_seconds``); every level above triples; MAIN (depth 0) is
#: excluded — its budget is the caller's own turn.
_DEPTH_BUDGET_MULTIPLIER = 3.0


def resolve_run_timeout_seconds(timeout: float | None = None) -> float:
    """Return the effective run timeout, falling back to the global config default."""
    if timeout is not None and timeout > 0:
        return timeout
    return get_config().run_timeout_seconds


def _requester_run(requester_session_key: str):
    """The requester's own run record, or ``None`` when the requester is MAIN.

    A child session key resolves through the registry; a main session has no
    run record (its children are depth 1 by definition). A registry failure is
    treated as MAIN — the ladder then degrades one rung shallower, never
    deeper.
    """
    key = (requester_session_key or "").strip()
    if not key or ":subagent:" not in key:
        return None
    try:
        from ..registry.queries import get_run_by_child_session_key

        return get_run_by_child_session_key(key)
    except Exception:
        return None


def remaining_parent_run_budget(requester_session_key: str) -> float | None:
    """Seconds left on the PARENT run's wall clock, or ``None``.

    A grandchild's spawn reads its parent run by the requester's child session
    key: the frozen ``run_timeout_seconds`` plus the monotonic ``started_at``
    give the remaining subtree budget. ``None`` means "not a child run" (a
    main-agent requester), "no stored timeout" (a legacy/unbounded run), or a
    registry read failure — the caller then keeps the regular timeout.
    """
    parent = _requester_run(requester_session_key)
    if parent is None:
        return None
    timeout = parent.run_timeout_seconds
    started = parent.execution.started_at
    if not timeout or timeout <= 0 or started is None:
        return None
    return float(started) + float(timeout) - time.monotonic()


def resolve_run_timeout_for_requester(
    requester_session_key: str, timeout: float | None = None
) -> float:
    """Effective timeout for a child spawn: the DEPTH LADDER, then the parent cap.

    Depth ladder (leaves get the configured default, every level above triples,
    main_agent excluded — its budget is the caller's own turn): a run at depth
    ``d`` gets ``run_timeout_seconds * 3 ** (max_spawn_depth - d)``. With the
    defaults (leaf 1800 s, max depth 2) a depth-1 child gets 5400 s; a model
    override via ``SHERRY_EVAL_CHILD_RUN_TIMEOUT_S`` reshapes the leaf and every
    level above it. An explicit ``timeout`` still wins — the ladder shapes
    only the default. The subtree rule applies afterwards unchanged: a
    descendant may never outlive its parent's remaining budget, with a 1 s
    fail-fast floor.
    """
    cfg = get_config()
    explicit = timeout is not None and timeout > 0
    if explicit:
        base = float(timeout)
    else:
        parent = _requester_run(requester_session_key)
        requester_depth = parent.depth if parent is not None else 0
        child_depth = requester_depth + 1
        levels_above_leaf = max(cfg.max_spawn_depth - child_depth, 0)
        base = cfg.run_timeout_seconds * (_DEPTH_BUDGET_MULTIPLIER**levels_above_leaf)
    remaining = remaining_parent_run_budget(requester_session_key)
    if remaining is None:
        return base
    return max(1.0, min(base, remaining))


def clamp_run_timeout_seconds(timeout: float | None) -> float | None:
    """Clamp a MODEL-provided wall-clock timeout to the configured bounds.

    Only the LLM tool's input is clamped: ``resolve_run_timeout_seconds`` is the
    shared resolver for TaskFlow dispatch, steer and the followup sweep, and a
    clamp there would silently cut off callers with their own intent.
    ``None`` / non-positive reads as "not set" and stays ``None``, which the
    resolver turns into the global default.
    """
    if timeout is None or timeout <= 0:
        return None
    config = get_config()
    return max(config.run_timeout_min_seconds, min(config.run_timeout_max_seconds, timeout))


def resolve_configured_subagent_run_timeout_seconds(
    run_timeout_seconds: float | None = None,
) -> float:
    """Alias for resolve_run_timeout_seconds kept for backward compatibility."""
    if run_timeout_seconds is not None and run_timeout_seconds > 0:
        return run_timeout_seconds
    return get_config().run_timeout_seconds


def resolve_run_deadline_ms(
    started_at: float | None, timeout_seconds: float | None = None
) -> float | None:
    """Compute the absolute deadline timestamp from a start time and timeout."""
    if started_at is None:
        return None
    timeout = resolve_run_timeout_seconds(timeout_seconds)
    return started_at + timeout


def resolve_role_model_profile(
    requester_session_key: str | None, functional_role: object
) -> dict[str, str] | None:
    """The requester session's model profile for one functional role, or ``None``.

    The 预设-子代理模型 tab stores ``AGENT_CONFIG["subagent_models"]`` as
    ``{role: {model, provider?, api_key?, base_url?}}`` (written by
    ``PUT /sessions/agent_config``); a role WITHOUT an entry follows its
    ``model_tier`` as before. Mem-only (the spawn runs on the agent loop) and
    fail-open: an unreadable register or a malformed payload means "use the
    tier", never "spawn without a model".
    """
    if not requester_session_key:
        return None
    try:
        from runtime import StateKey, state_register_mem

        from ..registry.session_state import normalize_session_key

        raw = state_register_mem.get_state(
            normalize_session_key(requester_session_key), StateKey.AGENT_CONFIG, None
        )
        if not isinstance(raw, dict):
            return None
        models = raw.get("subagent_models")
        if not isinstance(models, dict):
            return None
        role_value = getattr(functional_role, "value", str(functional_role))
        profile = models.get(role_value)
        if (
            isinstance(profile, dict)
            and isinstance(profile.get("model"), str)
            and profile["model"].strip()
        ):
            return {str(k): str(v) for k, v in profile.items() if isinstance(v, str)}
        return None
    except Exception:  # noqa: BLE001 - never break a spawn over the config read
        from loguru import logger

        logger.exception("resolve_role_model_profile: config read failed; using the role tier")
        return None


class ModelThinkingPlan:
    """Resolved model and thinking configuration for a sub-agent run."""

    def __init__(
        self,
        resolved_model: str | None = None,
        thinking_override: str | None = None,
        model_applied: bool = False,
        initial_session_patch: dict | None = None,
        error: str | None = None,
        model_tier: str | None = None,
    ):
        self.resolved_model = resolved_model
        self.thinking_override = thinking_override
        self.model_applied = model_applied
        self.initial_session_patch = initial_session_patch or {}
        self.error = error
        self.model_tier = model_tier


def resolve_model_and_thinking_plan(
    model_override: str | None = None,
    thinking_override_raw: str | None = None,
    requester_thinking: str | None = None,
    target_agent_thinking: str | None = None,
    model_tier: str | None = None,
) -> ModelThinkingPlan:
    """Resolve the effective model and thinking level using the override hierarchy: explicit → requester → target agent default.

    ``model_tier`` is the functional-role hint ("main" | "auxiliary" |
    "inherit") forwarded by the spawn pipeline so ``_build_child_agent`` can
    pick the LLM by role instead of depth. ``None`` means no functional-role
    override (pre-migration depth-based selection).
    """
    from .text_helpers import resolve_thinking_override, resolve_initial_session_patch

    resolved_model = None
    model_applied = False

    if model_override and model_override.strip():
        resolved_model = model_override.strip()
        model_applied = True

    thinking_override = resolve_thinking_override(
        requester_thinking=requester_thinking,
        target_agent_thinking=target_agent_thinking,
        explicit_override=thinking_override_raw,
    )

    initial_session_patch = resolve_initial_session_patch(thinking_override)

    return ModelThinkingPlan(
        resolved_model=resolved_model,
        thinking_override=thinking_override,
        model_applied=model_applied,
        initial_session_patch=initial_session_patch,
        model_tier=model_tier,
    )
