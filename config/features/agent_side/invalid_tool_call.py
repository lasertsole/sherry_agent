"""Invalid tool-call salvage settings (syntax repair + bounded resend)."""

from typing import TypedDict


class InvalidToolCallConfig(TypedDict):
    """Invalid tool-call salvage settings (syntax repair + bounded resend)."""

    enabled: bool
    log_args_chars: int
    resend_max_retries: int


INVALID_TOOL_CALL: InvalidToolCallConfig = {
    # The salvage path always runs in a shipped build; the switch exists so an
    # operator can disable the repair + resend pair without a code change.
    "enabled": True,
    # Raw-args preview carried into the WARNING log and the trajectory event.
    "log_args_chars": 2000,
    # Bounded re-calls after a repair miss (the only shape where the turn would
    # otherwise end on an invalid tool call). 0 disables the resend stage.
    "resend_max_retries": 2,
}
