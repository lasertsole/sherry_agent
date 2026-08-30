"""Storage backend class factory.

Resolves a storage backend name (e.g. ``"SNKVKVStorage"``) to its concrete
implementation class through the ``STORAGES`` registry.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

from graph_rag.vendored_lightrag.kg import STORAGES


def get_storage_class(storage_name: str) -> Callable[..., Any]:
    """Return the storage backend class for ``storage_name``."""
    # This vendored LightRAG is SNKV-only, so every backend is resolved lazily
    # through the ``STORAGES`` registry. ``STORAGES`` values are absolute module
    # paths (e.g. ``graph_rag.vendored_lightrag.kg.snkv_kv_impl``), so the
    # ``package`` anchor is ignored and the import resolves under the active
    # ``graph_rag`` module identity.
    import_path = STORAGES[storage_name]
    module = importlib.import_module(import_path, package="lightrag")
    return getattr(module, storage_name)
