"""User-configurable lifecycle hooks (read-only Step 1 dispatch)."""

from .core import HookOutcome, UserHooksMiddleware

__all__ = ["HookOutcome", "UserHooksMiddleware"]
