"""Model-budget retry state (prototype ``src/app/retry_health.ts``)."""

from __future__ import annotations

from dataclasses import dataclass

MAX_RETRIES = 3


@dataclass
class RetryState:
    """Consecutive retry state for one role."""

    attempts: int = 0
    last_error: str = ""

    @property
    def exhausted(self) -> bool:
        """Whether the retry budget is spent."""
        return self.attempts >= MAX_RETRIES


class RetryHealth:
    """Track transient model failures; success resets the counter."""

    def __init__(self) -> None:
        self._states: dict[str, RetryState] = {}

    def record(self, role: str, error: str) -> RetryState:
        """Record a failure and return the updated state."""
        state = self._states.setdefault(role, RetryState())
        state.attempts += 1
        state.last_error = error[:200]
        return state

    def clear(self, role: str) -> None:
        """Reset a role after a successful turn."""
        self._states.pop(role, None)

    def state(self, role: str) -> RetryState:
        """Return a role's current state."""
        return self._states.get(role, RetryState())

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a panel-friendly snapshot."""
        return {
            role: {"attempts": state.attempts, "exhausted": state.exhausted}
            for role, state in self._states.items()
        }


__all__ = ["MAX_RETRIES", "RetryHealth", "RetryState"]
