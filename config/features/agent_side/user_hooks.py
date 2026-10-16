"""User-configurable lifecycle hooks (``sherry.jsonc`` ``hooks`` section).

The knobs bounding one hook dispatch. The hook SPECS themselves (command,
matcher, timeout) are user configuration and live in ``sherry.jsonc`` — see
``config/sherry_settings.py::load_sherry_hooks`` — while every numeric bound
lives here, per the repo's config rule.

Step 1 of the plan is read-only/notification dispatch: a hook can observe
(and audit) but never block, change arguments or inject context. The switch
therefore defaults to OFF — a tracked config file must not be able to make the
agent execute commands on its own.
"""

from typing import TypedDict


class UserHooksConfig(TypedDict):
    """Bounds for user lifecycle-hook dispatch."""

    #: Master switch (``hooks.enabled`` in sherry.jsonc). OFF by default: hooks
    #: execute operator-supplied commands, so enabling them is a deliberate act.
    enabled: bool
    #: Timeout applied to a hook that declares none.
    default_timeout_ms: int
    #: Ceiling a declared ``timeout_ms`` is clamped to.
    max_timeout_ms: int
    #: Hooks dispatched per event per occurrence (config order, the rest skipped).
    max_hooks_per_event: int
    #: Cap on the stdout read from one hook (JSON outcome parsing included).
    max_output_bytes: int
    #: Cap on the stderr text that reaches the LOG (never the model).
    stderr_preview_chars: int
    #: Cap on the tool-result preview handed to a PostToolUse hook.
    result_preview_chars: int
    #: Cap on the error preview handed to a PostToolUseFailure hook.
    error_preview_chars: int
    #: Cap on the turn's response preview handed to a Stop hook.
    response_preview_chars: int


USER_HOOKS: UserHooksConfig = {
    "enabled": False,
    "default_timeout_ms": 5_000,
    "max_timeout_ms": 30_000,
    "max_hooks_per_event": 10,
    "max_output_bytes": 32_768,
    "stderr_preview_chars": 4_000,
    "result_preview_chars": 2_000,
    "error_preview_chars": 2_000,
    "response_preview_chars": 2_000,
}
