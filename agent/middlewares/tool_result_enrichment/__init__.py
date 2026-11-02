"""Tool-result enrichment middleware and default injector registry."""

from .core import (
    ToolResultInjector as ToolResultInjector,
    clear_session as clear_session,
    register as register,
    registered_injectors as registered_injectors,
    resolve_injections as resolve_injections,
)
from .injectors import (
    clear_hook_context as clear_hook_context,
    stash_hook_context as stash_hook_context,
)
from .middleware import ToolResultEnrichmentMiddleware as ToolResultEnrichmentMiddleware
from .sources import register_default_injectors as _register_default_injectors

_register_default_injectors()

__all__ = [
    "ToolResultEnrichmentMiddleware",
    "ToolResultInjector",
    "clear_hook_context",
    "clear_session",
    "register",
    "registered_injectors",
    "resolve_injections",
    "stash_hook_context",
]
