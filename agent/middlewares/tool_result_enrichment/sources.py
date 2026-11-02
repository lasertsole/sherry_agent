"""Default tool-result injectors."""

from __future__ import annotations

from .core import ToolResultInjector, register
from .injectors import build_hook_context

__all__ = ["register_default_injectors"]


def register_default_injectors() -> None:
    """Register the default injectors (idempotent — re-register replaces)."""
    register(
        ToolResultInjector(
            injector_id="hook_context",
            builder=build_hook_context,
            priority=60,
            tools_filter=None,
            only_if_changed=True,
        )
    )
