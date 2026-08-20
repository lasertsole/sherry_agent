"""Tests for the vendored SNKV storage backends wired into LightRAG.

The ``rag_anything`` lightweight-RAG module was refactored (per
hash-anu/lightrag-snkv) to store its data on snkv backends instead of the
standard LightRAG JSON/NanoVectorDB/NetworkX storages, while keeping
``lightrag-hku`` 1.5.2 installed.  These tests exercise:

1. Registration: the four SNKV class names land in ``lightrag.kg`` registries
   and are lazily resolvable via ``get_storage_class``.
2. Concrete init: a ``LightRAG`` instance built with the SNKV storage names
   actually gets SNKV-backed backend instances.
3. The production import path: the deep dotted ``skills...rag_anything``
   import (as used by ``rag_index.py``/``rag_query.py``) works from the repo
   root without a ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure the repo root is on sys.path the way the entry scripts do.  The
# ``rag_anything/__init__.py`` bootstrap is what makes the short module name
# resolvable afterwards.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The vendored snkv_storage lives under the deep package path.
_PKG = "skills.builtin.core.multimodal_rag.scripts.rag_anything.snkv_storage"

from lightrag import LightRAG  # noqa: E402
from lightrag.kg.factory import get_storage_class  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402


def _register():
    reg = importlib.import_module(f"{_PKG}.register")
    reg._REGISTERED = False  # reset idempotency guard for self-contained tests
    reg.register()


async def _stub_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
    return "ok"


async def _stub_embed(texts):
    return np.random.rand(len(texts), 3).astype("float32")


def _snkv_module(suffix: str) -> str:
    """Expected module id for a given SNKV impl under the active identity."""
    return f"{_PKG}.snkv_{suffix}_impl"


def test_registration_resolves_all_snkv_classes():
    """register() injects the 4 SNKV names and the factory can load them."""
    _register()

    for name, suffix in (
        ("SNKVKVStorage", "kv"),
        ("SNKVVectorStorage", "vector"),
        ("SNKVGraphStorage", "graph"),
        ("SNKVDocStatusStorage", "doc_status"),
    ):
        cls = get_storage_class(name)
        assert cls.__name__ == name
        # Must live under the ACTIVE module identity (no duplicate import)
        assert cls.__module__ == _snkv_module(suffix)


@pytest.mark.asyncio
async def test_lightrag_initializes_with_snkv_backends(tmp_path):
    """A LightRAG built with SNKV names gets concrete SNKV backend instances."""
    _register()

    working_dir = (tmp_path / "rag_store").as_posix()
    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=_stub_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=3, max_token_size=128, func=_stub_embed
        ),
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
    )

    await rag.initialize_storages()

    assert type(rag.text_chunks).__name__ == "SNKVKVStorage"
    assert type(rag.chunk_entity_relation_graph).__name__ == "SNKVGraphStorage"
    assert type(rag.entities_vdb).__name__ == "SNKVVectorStorage"
    assert type(rag.doc_status).__name__ == "SNKVDocStatusStorage"


def test_production_dotted_import_path_works():
    """The deep dotted import (as used by entry scripts) resolves cleanly."""
    mod = importlib.import_module(
        "skills.builtin.core.multimodal_rag.scripts.rag_anything"
    )
    assert hasattr(mod, "get_lightrag")
    # Exactly ONE module identity for the package (no short-name duplicate)
    assert "rag_anything" not in sys.modules
