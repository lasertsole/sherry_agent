# SNKV-only storage registries.
#
# The vendored LightRAG is locked to the SNKV storage family (embedded
# SQLite + vector stores) — no Redis/Postgres/Mongo/Neo4J/Milvus/etc.  The
# four SNKV backends are NATIVE storage implementations of this package,
# co-located in this ``kg`` package as ``snkv_*_impl.py``.  They are baked
# directly into the registries below — no runtime ``register()`` injection.
#
# The backends are resolved lazily (on first ``LightRAG`` construction)
# through ``STAGES[<storage_name>]``, whose value is an absolute module path
# into the same loaded ``graph_rag`` package via ``kg.factory``.
STORAGE_IMPLEMENTATIONS = {
    "KV_STORAGE": {
        "implementations": ["SNKVKVStorage"],
        "required_methods": ["get_by_id", "upsert"],
    },
    "GRAPH_STORAGE": {
        "implementations": ["SNKVGraphStorage"],
        "required_methods": ["upsert_node", "upsert_edge"],
    },
    "VECTOR_STORAGE": {
        "implementations": ["SNKVVectorStorage"],
        "required_methods": ["query", "upsert"],
    },
    "DOC_STATUS_STORAGE": {
        "implementations": ["SNKVDocStatusStorage"],
        "required_methods": ["get_docs_by_status"],
    },
}

# Storage implementation environment variable without default value.
# The SNKV family needs no external services — embedded SQLite only — so
# every entry is an empty list.
STORAGE_ENV_REQUIREMENTS: dict[str, list[str]] = {
    "SNKVKVStorage": [],
    "SNKVVectorStorage": [],
    "SNKVGraphStorage": [],
    "SNKVDocStatusStorage": [],
}

# Storage implementation module mapping. These absolute module paths resolve
# to the co-located ``snkv_*_impl.py`` native modules in this ``kg`` package.
STORAGES = {
    "SNKVKVStorage": "graph_rag.vendored_lightrag.kg.snkv_kv_impl",
    "SNKVVectorStorage": "graph_rag.vendored_lightrag.kg.snkv_vector_impl",
    "SNKVGraphStorage": "graph_rag.vendored_lightrag.kg.snkv_graph_impl",
    "SNKVDocStatusStorage": "graph_rag.vendored_lightrag.kg.snkv_doc_status_impl",
}


def verify_storage_implementation(storage_type: str, storage_name: str) -> None:
    """Verify if storage implementation is compatible with specified storage type

    Args:
        storage_type: Storage type (KV_STORAGE, GRAPH_STORAGE etc.)
        storage_name: Storage implementation name

    Raises:
        ValueError: If storage implementation is incompatible or missing required methods
    """
    if storage_type not in STORAGE_IMPLEMENTATIONS:
        raise ValueError(f"Unknown storage type: {storage_type}")

    storage_info = STORAGE_IMPLEMENTATIONS[storage_type]
    if storage_name not in storage_info["implementations"]:
        raise ValueError(
            f"Storage implementation '{storage_name}' is not compatible with {storage_type}. "
            f"Compatible implementations are: {', '.join(storage_info['implementations'])}"
        )
