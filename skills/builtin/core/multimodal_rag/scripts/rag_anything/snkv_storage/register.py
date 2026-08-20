"""One-call registration of SNKV storage backends into LightRAG.

Usage (before creating any LightRAG instance):

    from rag_anything.snkv_storage.register import register
    register()

    rag = LightRAG(
        working_dir="C:/absolute/path/to/rag/store",  # MUST be absolute (see note)
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
        ...
    )

.. note::
   ``working_dir`` defaults to ``./rag_storage`` in lightrag and is resolved
   relative to the **current working directory** — so a relative value (e.g.
   ``./rag_storage``) scatters the RAG data into whatever directory the
   process launches from. **Always pass an absolute path.** The production
   entry points use ``SRC_DIR / "rag" / "store"`` (absolute) so the store
   reliably lands under ``src/rag/store``.
"""
from __future__ import annotations

_REGISTERED = False


def _backend_base() -> str:
    """Return the active dotted prefix for the snkv_storage submodules.

    The package can be imported either as ``rag_anything`` (when the
    ``scripts/`` folder is on ``sys.path``, e.g. running a script) or as a
    deep dotted path starting with ``skills`` (when reached from the repo
    root).  We must register module paths that resolve under the SAME module
    identity that is already loaded -- otherwise LightRAG's lazy
    ``importlib.import_module`` would create a second, duplicate copy of the
    ``rag_anything`` package (re-running ``core.py`` side effects and
    double-registering parsers).

    The current package object knows its own active name via ``__package__``,
    so the sibling ``snkv_storage`` submodules live at
    ``<this package's parent>.snkv_storage.*``.
    """
    # __package__ is e.g.
    #   "rag_anything.snkv_storage"                      -> parent "rag_anything"
    #   "skills.builtin.core.multimodal_rag.scripts.rag_anything.snkv_storage"
    #                                                     -> parent "skills...rag_anything"
    parent = __package__ or "rag_anything.snkv_storage"
    pkg_parent = parent.rpartition(".")[0]
    return f"{pkg_parent}.snkv_storage"


def register() -> None:
    """Inject SNKV class names into LightRAG's storage registries."""
    global _REGISTERED
    if _REGISTERED:
        return

    from lightrag.kg import STORAGE_ENV_REQUIREMENTS, STORAGE_IMPLEMENTATIONS, STORAGES

    base = _backend_base()

    # Module paths for lazy import (absolute, not relative to lightrag)
    STORAGES["SNKVKVStorage"] = f"{base}.snkv_kv_impl"
    STORAGES["SNKVVectorStorage"] = f"{base}.snkv_vector_impl"
    STORAGES["SNKVGraphStorage"] = f"{base}.snkv_graph_impl"
    STORAGES["SNKVDocStatusStorage"] = f"{base}.snkv_doc_status_impl"

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
