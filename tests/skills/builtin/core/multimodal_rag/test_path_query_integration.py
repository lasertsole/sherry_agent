"""Integration test: the ``path`` query mode over real SNKV storages.

The unit suite pins the algorithm with fakes; this test runs the same search
through ``operate.kg_query`` and the vendored LightRAG dispatch against an
SNKV-backed graph + entity/vector storage, without any LLM call (keywords are
supplied, and only the context is requested).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from graph_rag.vendored_lightrag import LightRAG, QueryParam  # noqa: E402
from graph_rag.vendored_lightrag.utils import EmbeddingFunc  # noqa: E402

pytestmark = [pytest.mark.integration]


async def _stub_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
    return "ok"


async def _stub_embed(texts):
    out = []
    for text in texts:
        lowered = text.lower()
        if "nova" in lowered:
            out.append([1.0, 0.0, 0.0])
        elif "kepler" in lowered:
            out.append([0.8, 0.6, 0.0])
        elif "titan" in lowered:
            out.append([0.6, 0.8, 0.0])
        elif "fenwick" in lowered:
            out.append([0.0, 1.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return np.asarray(out, dtype="float32")


def _build_rag(working_dir: str) -> LightRAG:
    return LightRAG(
        working_dir=working_dir,
        llm_model_func=_stub_llm,
        embedding_func=EmbeddingFunc(embedding_dim=3, max_token_size=128, func=_stub_embed),
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
        cosine_better_than_threshold=0.0,
    )


async def _seed(rag: LightRAG) -> None:
    graph = rag.chunk_entity_relation_graph
    nodes = {
        "Nova Aerospace": "Nova Aerospace operates the Kepler-7 launch pad.",
        "Kepler-7": "The Kepler-7 launch pad uses beryllium clamps machined by Titan Alloys.",
        "Titan Alloys": "Titan Alloys is a wholly owned subsidiary of Fenwick Holdings.",
        "Fenwick Holdings": "Fenwick Holdings acquired Titan Alloys in 2019.",
    }
    for name, description in nodes.items():
        await graph.upsert_node(
            name,
            {
                "entity_type": "organization",
                "description": description,
                "source_id": f"chunk-{name}",
                "file_path": "corpus.txt",
            },
        )
    edges = [
        ("Nova Aerospace", "Kepler-7", "Nova Aerospace operates the Kepler-7 launch pad."),
        ("Kepler-7", "Titan Alloys", "Kepler-7 uses clamps machined by Titan Alloys."),
        ("Titan Alloys", "Fenwick Holdings", "Fenwick Holdings owns Titan Alloys."),
    ]
    for src, tgt, description in edges:
        await graph.upsert_edge(
            src,
            tgt,
            {
                "description": description,
                "keywords": "operation,supply",
                "weight": 1.0,
                "source_id": f"chunk-{src}-{tgt}",
                "file_path": "corpus.txt",
            },
        )

    await rag.entities_vdb.upsert(
        {
            name: {
                "content": f"{name}: {description}",
                "entity_name": name,
                "source_id": f"chunk-{name}",
                "entity_type": "organization",
                "file_path": "corpus.txt",
            }
            for name, description in nodes.items()
        }
    )
    await rag.text_chunks.upsert(
        {
            f"chunk-{src}-{tgt}": {
                "content": description,
                "file_path": "corpus.txt",
                "tokens": 12,
                "chunk_order_index": index,
            }
            for index, (src, tgt, description) in enumerate(edges)
        }
    )


@pytest.mark.asyncio
async def test_path_mode_returns_the_bridge_chain(tmp_path):
    rag = _build_rag((tmp_path / "rag_store").as_posix())
    await rag.initialize_storages()
    try:
        await _seed(rag)
        params = QueryParam(
            mode="path",
            only_need_context=True,
            ll_keywords=["Nova Aerospace", "Fenwick Holdings"],
            hl_keywords=[],
        )
        context = await rag.aquery(
            "How is Nova Aerospace connected to Fenwick Holdings?", param=params
        )
        context = str(context)

        assert "Explicit relationship paths" in context
        assert "Nova Aerospace → Kepler-7 → Titan Alloys → Fenwick Holdings" in context
        assert "Fenwick Holdings owns Titan Alloys." in context

        data = await rag.aquery_data(
            "How is Nova Aerospace connected to Fenwick Holdings?", param=params
        )
        assert data["status"] == "success"
        path_info = data["metadata"]["path_info"]
        assert path_info["used_beam"] is False
        assert path_info["paths"][0]["hops"] == 3
        assert path_info["paths"][0]["nodes"][-1] == "Fenwick Holdings"
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_mix_mode_auto_merges_path_context_for_relationship_queries(tmp_path):
    rag = _build_rag((tmp_path / "rag_store").as_posix())
    await rag.initialize_storages()
    try:
        await _seed(rag)
        params = QueryParam(
            mode="mix",
            only_need_context=True,
            ll_keywords=["Nova Aerospace", "Fenwick Holdings"],
            hl_keywords=["Nova Aerospace", "Fenwick Holdings"],
        )
        context = str(
            await rag.aquery("How is Nova Aerospace connected to Fenwick Holdings?", param=params)
        )

        assert "Explicit relationship paths" in context
        assert "Nova Aerospace → Kepler-7 → Titan Alloys → Fenwick Holdings" in context
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_non_relationship_mix_query_skips_the_path_section(tmp_path):
    rag = _build_rag((tmp_path / "rag_store").as_posix())
    await rag.initialize_storages()
    try:
        await _seed(rag)
        params = QueryParam(
            mode="mix",
            only_need_context=True,
            ll_keywords=["Nova Aerospace"],
            hl_keywords=[],
        )
        context = str(await rag.aquery("What does Nova Aerospace operate?", param=params))

        assert "Explicit relationship paths" not in context
    finally:
        await rag.finalize_storages()
