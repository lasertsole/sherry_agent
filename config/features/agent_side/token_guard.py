"""MIN_MAX_TOKEN guard: enforces both agents' context window >= 200K."""

MIN_REQUIRED_MAX_TOKEN: int = 200_000

#: Business floor for a per-request OUTPUT cap (distinct from the context
#: window floor above). Thinking-mode reasoning and the visible answer share
#: this budget, so anything below 8K truncates the reply.
MIN_MAX_OUTPUT_TOKEN: int = 8_192


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN.

    Args:
        key: The env var name (e.g. 'MAIN_LLM_MAX_TOKEN'), used in the error message.
        value: The parsed integer value, or None when unset.
    """
    if value is None:
        raise TokenGuardError(f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (200K)")
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (200K); "
            f"agent startup is blocked"
        )


def assert_max_output_valid(key: str, value: int | None) -> None:
    """Raise when a configured max-output cap is below the 8192 business floor.

    An UNSET value (``None``) is valid and means "do not send the field", so
    the provider applies its own default (M3).
    """
    if value is None:
        return
    if value < MIN_MAX_OUTPUT_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_MAX_OUTPUT_TOKEN} (8K); "
            "thinking-mode reasoning and the answer share this budget, so a "
            "smaller value truncates the reply"
        )
