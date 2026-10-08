"""Idempotency key generation for announce delivery deduplication."""


def build_idempotency_key(run_id: str, generation: int = 0, suffix: str | None = None) -> str:
    """Build a unique idempotency key from run_id, generation, and optional suffix."""
    key = f"subagent_announce:{run_id}:gen:{generation}"
    if suffix:
        key = f"{key}:{suffix}"
    return key
