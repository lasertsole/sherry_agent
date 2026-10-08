"""MIN_MAX_TOKEN guard: enforces both agents' context window >= 128K."""

MIN_REQUIRED_MAX_TOKEN: int = 131_072


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN.

    Args:
        key: The env var name (e.g. 'MAIN_LLM_MAX_TOKEN'), used in the error message.
        value: The parsed integer value, or None when unset.
    """
    if value is None:
        raise TokenGuardError(f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (128K)")
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (128K); "
            f"agent startup is blocked"
        )
