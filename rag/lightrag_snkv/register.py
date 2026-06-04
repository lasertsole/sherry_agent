"""One-call registration of SNKV storage backends into LightRAG.

Usage (before creating any LightRAG instance):

    from lightrag_snkv.register import register
    register()

    rag = LightRAG(
        working_dir="./rag_storage",
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
        ...
    )
"""
from __future__ import annotations
_REGISTERED = False


def register() -> None:
    """Inject SNKV class names into LightRAG's storage registries."""
    global _REGISTERED
    if _REGISTERED:
        return

    from lightrag.kg import STORAGE_ENV_REQUIREMENTS, STORAGE_IMPLEMENTATIONS, STORAGES

    # Module paths for lazy import (absolute, not relative to lightrag)
    STORAGES["SNKVKVStorage"] = "rag.lightrag_snkv.snkv_kv_impl"
    STORAGES["SNKVVectorStorage"] = "rag.lightrag_snkv.snkv_vector_impl"
    STORAGES["SNKVGraphStorage"] = "rag.lightrag_snkv.snkv_graph_impl"
    STORAGES["SNKVDocStatusStorage"] = "rag.lightrag_snkv.snkv_doc_status_impl"

    # Add to validation lists
    STORAGE_IMPLEMENTATIONS["KV_STORAGE"]["implementations"].append("SNKVKVStorage")
    STORAGE_IMPLEMENTATIONS["VECTOR_STORAGE"]["implementations"].append("SNKVVectorStorage")
    STORAGE_IMPLEMENTATIONS["GRAPH_STORAGE"]["implementations"].append("SNKVGraphStorage")
    STORAGE_IMPLEMENTATIONS["DOC_STATUS_STORAGE"]["implementations"].append(
        "SNKVDocStatusStorage"
    )

    # No external services required — embedded SQLite
    STORAGE_ENV_REQUIREMENTS["SNKVKVStorage"] = []
    STORAGE_ENV_REQUIREMENTS["SNKVVectorStorage"] = []
    STORAGE_ENV_REQUIREMENTS["SNKVGraphStorage"] = []
    STORAGE_ENV_REQUIREMENTS["SNKVDocStatusStorage"] = []

    _REGISTERED = True


def register_with_lightrag(rag) -> None:
    """Register SNKV and configure a LightRAG instance to use all 4 backends.

    Call BEFORE ``await rag.initialize_storages()``.

    Args:
        rag: An uninitialised LightRAG instance.
    """
    register()
    rag.kv_storage = "SNKVKVStorage"
    rag.vector_storage = "SNKVVectorStorage"
    rag.graph_storage = "SNKVGraphStorage"
    rag.doc_status_storage = "SNKVDocStatusStorage"
