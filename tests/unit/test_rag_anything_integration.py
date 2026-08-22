"""Integration test: ragAnything works on top of SNKV-backed LightRAG.

``raganything`` (the pip package) wraps a LightRAG instance underneath; our
vendored ``graph_rag.core.get_rag_anything()`` builds that LightRAG via
``get_lightrag()`` which now uses the SNKV storage backends.  This test patched
the heavy model-download/init paths and asserts the resulting RAGAnything wields
an SNKV-backed LightRAG.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Put the multimodal_rag skill's ``scripts/`` dir on sys.path so the short,
# canonical ``graph_rag`` module name resolves (matching the runtime entry
# scripts).  The vendored lightrag uses short absolute imports internally, so
# the deep dotted ``skills...`` path would create a second module identity with
# divergent registries -- deliberately avoided here.
SCRIPTS_DIR = (
    REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_CORE = "graph_rag.core"
_PKG_TOP = "graph_rag"
_PKG = "graph_rag.vendored_lightrag.kg"
_LR = "graph_rag.vendored_lightrag"

from graph_rag.vendored_lightrag import LightRAG  # noqa: E402
from graph_rag.vendored_lightrag.utils import EmbeddingFunc  # noqa: E402


async def _stub_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
    return "ok"


async def _stub_embed(texts):
    return np.random.rand(len(texts), 3).astype("float32")


def _build_snkv_lightrag(working_dir: str) -> LightRAG:
    """Build a LightRAG configured with the SNKV backends.

    Mimics ``get_lightrag()``: importing the vendored package is enough — the
    SNKV backends are native to the vendored ``lightrag.kg`` (baked directly
    into its registries, no runtime ``register()`` injection).    """
    importlib.import_module(_PKG_TOP)

    return LightRAG(
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


@pytest.mark.asyncio
async def test_rag_anything_receives_snkv_backed_lightrag(tmp_path):
    """get_rag_anything() wires the SNKV-backed LightRAG into RAGAnything."""
    core = importlib.import_module(_CORE)

    # Real LightRAG built with SNKV backends (native to the vendored LightRAG).
    working_dir = (tmp_path / "work").as_posix()
    real_lightrag = _build_snkv_lightrag(working_dir)

    # Recording fake for RAGAnything: assert what it received without running
    # its heavy __post_init__ (parser loading, atexit, model downloads).
    captured = {}

    class FakeRAGAnything:
        def __init__(self, lightrag=None, vision_model_func=None, config=None):
            captured["lightrag"] = lightrag
            captured["vision_model_func"] = vision_model_func
            captured["config"] = config

        def __repr__(self):
            return "<FakeRAGAnything>"

    mocks = {
        "ensure_mineru_models": MagicMock(),  # no model downloads (sync)
        "get_lightrag": AsyncMock(return_value=real_lightrag),
        "RAGAnything": FakeRAGAnything,
    }

    with (
        patch(f"{_CORE}.ensure_mineru_models", mocks["ensure_mineru_models"]),
        patch(f"{_PKG_TOP}.get_lightrag", mocks["get_lightrag"]),
        patch(f"{_CORE}.RAGAnything", mocks["RAGAnything"]),
    ):
        # Remove any cached instance so the factory path runs fresh.
        core._rag_anything = None
        result = await core.get_rag_anything(parser="fallback_txt")

    # The factory returned our fake.
    assert isinstance(result, FakeRAGAnything)

    # The LightRAG passed in must be the SNKV-backed one we built.
    assert captured["lightrag"] is real_lightrag
    assert type(real_lightrag.text_chunks).__name__ == "SNKVKVStorage"
    assert type(real_lightrag.chunk_entity_relation_graph).__name__ == "SNKVGraphStorage"
    assert type(real_lightrag.entities_vdb).__name__ == "SNKVVectorStorage"
    assert type(real_lightrag.doc_status).__name__ == "SNKVDocStatusStorage"

    # Vision model func was wired through.
    assert callable(captured["vision_model_func"])
    assert captured["config"] is not None
    assert captured["config"].parser == "fallback_txt"
