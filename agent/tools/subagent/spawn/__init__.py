"""Public API for the spawn pipeline — sub-agent creation, validation, and launch."""

from .core import spawn_subagent_direct, SpawnResult
from .depth import get_subagent_depth, validate_spawn_depth
from .target_policy import validate_target_policy
from .plan import resolve_run_timeout_seconds, resolve_model_and_thinking_plan, ModelThinkingPlan
from .system_prompt import build_subagent_system_prompt, build_active_subagents_section
from .initial_message import build_subagent_initial_user_message
from .inherited_tool_policy import apply_tool_policy, DEFAULT_SUBAGENT_BLOCKED_TOOLS
from .context import prepare_spawned_context
from .task_name import normalize_subagent_task_name
from .attachments import materialize_subagent_attachments
from .ownership import resolve_spawn_ownership, SubagentSpawnOwnership
from .accepted_note import resolve_spawn_accepted_note
from .thinking import resolve_thinking_override

__all__ = [
    "spawn_subagent_direct",
    "SpawnResult",
    "get_subagent_depth",
    "validate_spawn_depth",
    "validate_target_policy",
    "build_subagent_system_prompt",
    "build_active_subagents_section",
    "build_subagent_initial_user_message",
    "apply_tool_policy",
    "DEFAULT_SUBAGENT_BLOCKED_TOOLS",
    "resolve_spawn_ownership",
    "SubagentSpawnOwnership",
    "resolve_spawn_accepted_note",
    "resolve_thinking_override",
    "resolve_model_and_thinking_plan",
    "ModelThinkingPlan",
]
