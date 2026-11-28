"""Single-writer health entries (prototype ``src/infra/health.py``)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from superagent.limits import HEALTH_FAILURE_LIMIT

_RESERVED_NAMES = frozenset(
    {
        "runtime",
        "models",
        "evals",
        "assessment",
        "launch",
        "delivery",
        "relay",
        "notice",
        "reply",
        "worker",
        "github",
        "feishu",
        "history",
    }
)


class ClaimError(ValueError):
    """Raised when two owners claim the same health entry."""


class HealthBoard:
    """Health entries with claim-based ownership and failure debounce."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, int] = {}

    def claim(self, name: str, owner: str) -> None:
        """Register an owner; reserved names reject other owners."""
        previous = self._owners.get(name)
        if previous is not None and previous != owner:
            raise ClaimError(f"health entry {name!r} is already owned by {previous!r}")
        if name in _RESERVED_NAMES:
            if previous is None and owner != name:
                raise ClaimError(f"health entry {name!r} is reserved")
        elif name.startswith("sync:"):
            expected = name.split(":", 1)[1]
            if previous is None and owner != expected:
                raise ClaimError(f"health entry {name!r} is reserved")
        self._owners[name] = owner

    def report(self, name: str, state: dict[str, Any]) -> None:
        """Report state; errors surface after three consecutive failures."""
        value = dict(state)
        status = str(value.get("state", ""))
        if status == "error":
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            if failures < HEALTH_FAILURE_LIMIT and name != "worker":
                return
            value["failures"] = failures
        else:
            if self._failures.get(name, 0) > 0:
                self.clear(name)
                return
            self._failures[name] = 0
        self._states[name] = value

    def clear(self, name: str) -> None:
        """Clear an entry after a successful retry."""
        self._failures[name] = 0
        self._states.pop(name, None)

    def failures(self, name: str) -> int:
        """Return the consecutive failure count."""
        return self._failures.get(name, 0)

    def all(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of all visible entries."""
        return deepcopy(self._states)
