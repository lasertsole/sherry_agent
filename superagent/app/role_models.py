"""Role model resolution (prototype ``src/app/role_models.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoleModels:
    """Resolve the model used by each Sherry role."""

    default: str = ""
    eval_model: str = ""
    executor_model: str = ""

    def for_role(self, role: str) -> str:
        """Return the configured model for one role."""
        if role == "eval":
            return self.eval_model or self.default
        if role == "executor":
            return self.executor_model or self.default
        return self.default

    @classmethod
    def from_settings(cls, settings: Any) -> RoleModels:
        """Read roles from a settings object or dict."""

        def get(name: str) -> str:
            if isinstance(settings, dict):
                value = settings.get(name, "")
            else:
                value = getattr(settings, name, "")
            return str(value or "")

        return cls(
            default=get("model"),
            eval_model=get("eval_model"),
            executor_model=get("executor_model"),
        )


__all__ = ["RoleModels"]
