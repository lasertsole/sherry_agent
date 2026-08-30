"""Tests for the vendored SNKV storage backends wired into LightRAG.

The ``graph_rag`` lightweight-RAG module was refactored (per
hash-anu/lightrag-snkv) to store its data on snkv backends instead of any
other LightRAG storage family.  The vendored ``lightrag`` (under
``graph_rag/vendored_lightrag/``) is SNKV-only -- its ``kg`` registries
natively contain the four SNKV classes (baked directly into ``kg/__init__.py``,
no runtime ``register()`` injection).  These tests exercise:

1. Native registration: the four SNKV class names live in the vendored
   ``lightrag.kg`` registries and are lazily resolvable via ``get_storage_class``.
2. Concrete init: a ``LightRAG`` instance built with the SNKV storage names
   actually gets SNKV-backed backend instances.
3. The production import path: the short, canonical ``graph_rag`` import
   (as used by ``rag_index.py``/``rag_query.py`` -- ``scripts/`` is on
   ``sys.path``) resolves under a single module identity.

The vendored lightrag uses short, absolute imports internally
(``graph_rag.vendored_lightrag.kg``, etc.), so it is consistent only under the short
identity.  The deep dotted ``skills...graph_rag`` path is deliberately NOT
used here: it would create a second module identity whose registries diverge
from the ones the vendored internals read.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure the repo root is on sys.path the way the entry scripts do, and put the
# ``scripts/`` directory of the multimodal_rag skill on ``sys.path`` so the
# short, canonical module name ``graph_rag`` resolves (matching the runtime).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# The SNKV storage classes are NATIVE modules of the vendored ``vendored_lightrag``
# ``kg`` package: ``<...>/vendored_lightrag/kg/snkv_*_impl.py``.
_PKG = "graph_rag.vendored_lightrag.kg"
_LR = "graph_rag.vendored_lightrag"

from graph_rag.vendored_lightrag import LightRAG  # noqa: E402
from graph_rag.vendored_lightrag.kg.factory import (  # noqa: E402, F401
    get_storage_class,
)
from graph_rag.vendored_lightrag.kg import (  # noqa: E402
    STORAGE_IMPLEMENTATIONS,
)
from graph_rag.vendored_lightrag.utils import EmbeddingFunc  # noqa: E402


async def _stub_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
    return "ok"


async def _stub_embed(texts):
    return np.random.rand(len(texts), 3).astype("float32")


def _snkv_module(suffix: str) -> str:
    """Expected module id for a given SNKV impl under the active identity."""
    return f"{_PKG}.snkv_{suffix}_impl"


def test_registration_resolves_all_snkv_classes():
    """The 4 native SNKV names resolve lazily, and registries are SNKV-only."""
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

    # The vendored registries are SNKV-only: the 4 type keys are preserved (so
    # ``verify_storage_implementation`` still passes) and each holds exactly the
    # single SNKV implementation -- the non-SNKV families were fully stripped.
    impl_keys = set(STORAGE_IMPLEMENTATIONS)
    assert impl_keys == {"KV_STORAGE", "GRAPH_STORAGE", "VECTOR_STORAGE", "DOC_STATUS_STORAGE"}
    expected_impl = {
        "KV_STORAGE": ["SNKVKVStorage"],
        "VECTOR_STORAGE": ["SNKVVectorStorage"],
        "GRAPH_STORAGE": ["SNKVGraphStorage"],
        "DOC_STATUS_STORAGE": ["SNKVDocStatusStorage"],
    }
    for key, impl in STORAGE_IMPLEMENTATIONS.items():
        assert impl["implementations"] == expected_impl[key]


@pytest.mark.asyncio
async def test_lightrag_initializes_with_snkv_backends(tmp_path):
    """A LightRAG built with SNKV names gets concrete SNKV backend instances."""
    working_dir = (tmp_path / "rag_store").as_posix()
    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=_stub_llm,
        embedding_func=EmbeddingFunc(embedding_dim=3, max_token_size=128, func=_stub_embed),
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


def test_production_import_path_works():
    """The canonical short ``graph_rag`` import resolves cleanly."""
    mod = importlib.import_module("graph_rag")
    assert hasattr(mod, "get_lightrag")
    # Exactly ONE module identity for the package no matter how reached.
    assert "graph_rag.vendored_lightrag.kg" in sys.modules
    assert "skills.builtin.core.multimodal_rag.scripts.graph_rag" not in sys.modules
