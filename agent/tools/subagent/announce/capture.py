"""Capture and retry-read sub-agent completion output.

The completion output may lag behind the lifecycle status, so callers can
briefly wait before sending an empty or stale announcement.
"""

import asyncio
from loguru import logger
from ..types.registry import SubagentRunRecord


async def read_subagent_output_with_retry(
    child_session_key: str,
    max_wait_ms: int = 5000,
    retry_interval_ms: int = 500,
) -> str | None:
    """Read sub-agent output with retry: poll until non-empty text appears or bounded wait expires."""
    max_wait_ms = max(0, min(max_wait_ms, 15_000))  # Cap at 15s to prevent excessive waits
    waited_ms = 0.0
    result: str | None = None

    while waited_ms < max_wait_ms:
        result = await _read_output_from_registry(child_session_key)
        if result and result.strip():
            return result

        remaining_ms = max_wait_ms - waited_ms
        if remaining_ms <= 0:
            break

        sleep_ms = min(retry_interval_ms, remaining_ms)
        await asyncio.sleep(sleep_ms / 1000.0)
        waited_ms += sleep_ms

    return result


async def capture_subagent_completion_reply(
    child_session_key: str,
    wait_for_reply: bool = True,
    max_wait_ms: int = 5000,
    retry_interval_ms: int = 500,
) -> str | None:
    """Capture a sub-agent's completion reply with optional retry waiting.

    Tries an immediate read first; if empty and wait_for_reply is True,
    enters a retry loop until output appears or timeout is reached.
    """
    immediate = await _read_output_from_registry(child_session_key)
    if immediate and immediate.strip():
        return immediate

    if not wait_for_reply:
        return None

    return await read_subagent_output_with_retry(
        child_session_key=child_session_key,
        max_wait_ms=max_wait_ms,
        retry_interval_ms=retry_interval_ms,
    )


async def _read_output_from_registry(child_session_key: str) -> str | None:
    """Read result text from the registry by child session key."""
    from ..registry.read import get_run_by_child_session_key_readonly
    run = get_run_by_child_session_key_readonly(child_session_key)
    if run is None:
        return None
    return run.completion.result_text
