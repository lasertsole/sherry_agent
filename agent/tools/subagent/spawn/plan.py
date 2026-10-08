"""Run-plan resolution for sub-agents — timeout, model selection, and thinking level."""

from ..config import get_config


def resolve_run_timeout_seconds(timeout: float | None = None) -> float:
    """Return the effective run timeout, falling back to the global config default."""
    if timeout is not None and timeout > 0:
        return timeout
    return get_config().run_timeout_seconds


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


def split_model_ref(model_ref: str | None) -> tuple[str | None, str | None]:
    """Split a 'provider/model' reference into (provider, model); provider may be None."""
    if model_ref is None:
        return None, None
    if "/" in model_ref:
        provider, model = model_ref.split("/", 1)  # only split on the first slash
        return provider, model
    return None, model_ref


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
    from .thinking import resolve_thinking_override, resolve_initial_session_patch

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
