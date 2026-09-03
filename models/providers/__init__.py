"""Provider registry.

Exposes the declarative ``PROVIDERS`` metadata and the ``find_by_name``
lookup used by ``config.schema`` to route models to LLM providers.
"""

from .registry import PROVIDERS, ProviderSpec, find_by_name

__all__ = ["PROVIDERS", "ProviderSpec", "find_by_name"]
