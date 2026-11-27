"""FTS5 hybrid entity retrieval + vendored-directory merge regressions.

Covers the GraphRAG plan executed 2026-11-27:
- entities_vdb gets an optional FTS5 shadow index (unicode61 + trigram);
- relationships_vdb / chunks_vdb stay pure vector;
- QueryParam no longer accepts the removed ``bypass`` mode;
- all vendored modules now live directly under ``graph_rag`` and the bare
  ``lightrag`` / ``raganything`` sys.modules aliases still resolve.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from graph_rag import LightRAG, QueryParam  # noqa: E402
from graph_rag.fts_shadow_index import EntitiesFtsIndex, merge_fts_vector  # noqa: E402
from graph_rag.utils import EmbeddingFunc  # noqa: E402

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# FTS5 shadow index — pure sqlite, no model stack
# ---------------------------------------------------------------------------


def _index(tmp_path: Path) -> EntitiesFtsIndex:
    return EntitiesFtsIndex(tmp_path / "lightrag_fts.db")


def test_entities_fts_schema(tmp_path):
    idx = _index(tmp_path)
    try:
        names = {
            row[0] for row in idx._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"entities_fts", "entities_fts_trigram"} <= names
        assert idx.count() == 0
    finally:
        idx.close()


def test_entities_fts_upsert(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.sync_upsert({"e1": {"entity_name": "JSON parser", "content": "parses JSON"}})
        assert idx.count() == 1
        assert idx.search("parser") == [{"id": "e1", "distance": 1.0}]
    finally:
        idx.close()


def test_entities_fts_delete(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.sync_upsert({"e1": {"entity_name": "JSON parser", "content": "parses JSON"}})
        idx.sync_delete(["e1"])
        assert idx.count() == 0
        assert idx.search("parser") == []
    finally:
        idx.close()


def test_entities_fts_search_ascii(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.sync_upsert(
            {
                "e1": {"entity_name": "_strip_bom", "content": "remove BOM helper"},
                "e2": {"entity_name": "taint analysis", "content": "tracks tainted data"},
            }
        )
        assert [hit["id"] for hit in idx.search("_strip_bom")] == ["e1"]
    finally:
        idx.close()


def test_entities_fts_search_cjk(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.sync_upsert(
            {
                "e1": {"entity_name": "污点检测", "content": "分析数据流中的污点传播"},
                "e2": {"entity_name": "JSON parser", "content": "parses JSON"},
            }
        )
        assert [hit["id"] for hit in idx.search("污点检测")] == ["e1"]
    finally:
        idx.close()


def test_entities_fts_no_match(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.sync_upsert({"e1": {"entity_name": "JSON parser", "content": "parses JSON"}})
        assert idx.search("") == []
        assert idx.search("nonexistent-token-xyz") == []
    finally:
        idx.close()


def test_merge_weights():
    fts = [{"id": "exact", "distance": 1.0}]
    vec = [
        {"id": "exact", "distance": 0.5},
        {"id": "similar", "distance": 0.9},
    ]
    # FTS rank + vector score accumulate for the exact hit.
    merged = merge_fts_vector(fts, vec, fts_weight=0.4, vec_weight=0.6, top_k=2)
    assert [hit["id"] for hit in merged] == ["exact", "similar"]
    assert merged[0]["distance"] == pytest.approx(0.4 + 0.6 * 0.5)

    # Raising the FTS weight cannot demote the exact identifier hit.
    skewed = merge_fts_vector(fts, vec, fts_weight=2.0, vec_weight=0.1, top_k=2)
    assert skewed[0]["id"] == "exact"


def test_precise_entity_name_beats_semantic(tmp_path):
    """An exact identifier hit outranks a semantically similar description."""
    idx = _index(tmp_path)
    try:
        idx.sync_upsert(
            {
                "e-exact": {"entity_name": "_strip_bom", "content": "remove BOM helper"},
                "e-semantic": {"entity_name": "bom removal", "content": "removes BOM from text"},
            }
        )
        fts_hits = idx.search("_strip_bom")
        vec_hits = [
            {"id": "e-exact", "distance": 0.6},
            {"id": "e-semantic", "distance": 0.9},
        ]
        merged = merge_fts_vector(fts_hits, vec_hits, top_k=2)
        assert merged[0]["id"] == "e-exact"
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# SNKV storage integration — hermetic stub embeddings, real SNKV store
# ---------------------------------------------------------------------------


async def _stub_embed(texts):
    out = []
    for text in texts:
        lowered = str(text).lower()
        if "strip" in lowered or "bom" in lowered:
            out.append([1.0, 0.0, 0.0])
        elif "taint" in lowered or "污点" in lowered:
            out.append([0.0, 1.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return np.asarray(out, dtype="float32")


async def _stub_llm(prompt, system_prompt=None, history_messages=None, **kwargs):
    return "ok"


def _build_rag(tmp_path: Path, *, attach_fts: bool = False) -> LightRAG:
    rag = LightRAG(
        working_dir=(tmp_path / "rag_store").as_posix(),
        llm_model_func=_stub_llm,
        embedding_func=EmbeddingFunc(embedding_dim=3, max_token_size=128, func=_stub_embed),
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
        cosine_better_than_threshold=0.0,
    )
    if attach_fts:
        rag.entities_vdb.attach_fts(
            EntitiesFtsIndex(tmp_path / "lightrag_fts.db"), fts_weight=0.8, vec_weight=0.2
        )
    return rag


_ENTITIES = {
    "e-exact": {
        "content": "_strip_bom: remove BOM helper",
        "entity_name": "_strip_bom",
        "source_id": "chunk-a",
        "file_path": "src/util.py",
    },
    "e-taint": {
        "content": "污点检测: tracks tainted data",
        "entity_name": "污点检测",
        "source_id": "chunk-b",
        "file_path": "src/security.py",
    },
}


@pytest.mark.asyncio
async def test_snkv_entities_fts_sync(tmp_path):
    rag = _build_rag(tmp_path, attach_fts=True)
    await rag.initialize_storages()
    try:
        await rag.entities_vdb.upsert(_ENTITIES)
        assert rag.entities_vdb._fts.count() == 2
        assert {hit["id"] for hit in rag.entities_vdb._fts.search("_strip_bom")} == {"e-exact"}

        await rag.entities_vdb.delete(["e-exact"])
        assert rag.entities_vdb._fts.count() == 1
        assert rag.entities_vdb._fts.search("_strip_bom") == []
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_query_mixed_entities(tmp_path):
    rag = _build_rag(tmp_path, attach_fts=True)
    await rag.initialize_storages()
    try:
        await rag.entities_vdb.upsert(_ENTITIES)
        hits = await rag.entities_vdb.query("污点检测", top_k=2)
        assert hits
        assert hits[0]["id"] == "e-taint"
        # FTS rows only carry id/rank; merged hits must keep vector metadata.
        assert hits[0]["entity_name"] == "污点检测"
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_query_pure_vector_relationships(tmp_path):
    rag = _build_rag(tmp_path, attach_fts=True)
    await rag.initialize_storages()
    try:
        assert rag.relationships_vdb._fts is None
        assert rag.relationships_vdb._fts_weight == 0.4  # untouched default
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_query_pure_vector_chunks(tmp_path):
    rag = _build_rag(tmp_path, attach_fts=True)
    await rag.initialize_storages()
    try:
        assert rag.chunks_vdb._fts is None
        assert rag.chunks_vdb._fts_weight == 0.4
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_fts_disabled_pure_vector(tmp_path):
    """Without attach_fts the entity store behaves exactly as before."""
    rag = _build_rag(tmp_path, attach_fts=False)
    await rag.initialize_storages()
    try:
        await rag.entities_vdb.upsert(_ENTITIES)
        assert rag.entities_vdb._fts is None
        hits = await rag.entities_vdb.query("_strip_bom", top_k=2)
        # Pure vector recall returns both stored entities (the stub embeddings
        # are within the 0.0 cosine threshold); keyword recall is not involved.
        assert {hit["id"] for hit in hits} == {"e-exact", "e-taint"}
    finally:
        await rag.finalize_storages()


# ---------------------------------------------------------------------------
# bypass removal
# ---------------------------------------------------------------------------


def test_bypass_mode_removed():
    modes = QueryParam.__annotations__["mode"]
    assert "bypass" not in str(modes)
    assert QueryParam().mode == "mix"


def test_bypass_mode_rejected():
    with pytest.raises(ValueError, match="unsupported query mode"):
        QueryParam(mode="bypass")


# ---------------------------------------------------------------------------
# Directory merge regressions
# ---------------------------------------------------------------------------


def test_import_paths_resolve():
    package = REPO_ROOT / "skills/builtin/core/multimodal_rag/scripts/graph_rag"
    stale = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "vendored_lightrag" in text or "vendored_raganything" in text:
            stale.append(str(path.relative_to(package)))
    assert stale == []

    for mod in (
        "graph_rag",
        "graph_rag.lightrag",
        "graph_rag.lightrag_base",
        "graph_rag.operate",
        "graph_rag.kg.factory",
        "graph_rag.kg.snkv_vector_impl",
        "graph_rag.raganything",
        "graph_rag.fts_shadow_index",
        "graph_rag.sherry_adapter",
    ):
        assert importlib.import_module(mod) is not None


def test_sys_modules_alias_raganything():
    import raganything

    vendored = importlib.import_module("graph_rag.raganything")
    assert Path(raganything.__file__).parent == Path(vendored.__file__).parent
    assert "RAGAnything" in dir(raganything)


def test_sys_modules_alias_lightrag():
    import lightrag

    graph_rag = importlib.import_module("graph_rag")
    assert lightrag is graph_rag
    assert lightrag.LightRAG is graph_rag.LightRAG
    assert lightrag.kg is importlib.import_module("graph_rag.kg")


@pytest.mark.asyncio
async def test_lightrag_singleton_init(tmp_path, monkeypatch):
    import graph_rag.sherry_adapter as adapter
    from config.features import GRAPH_RAG_PATH

    monkeypatch.setattr(adapter, "SRC_DIR", tmp_path / "src")
    monkeypatch.setitem(GRAPH_RAG_PATH, "fts_hybrid_enabled", True)

    rag = await adapter.get_lightrag()
    try:
        assert rag.entities_vdb._fts is not None
        assert rag.relationships_vdb._fts is None
        assert rag.chunks_vdb._fts is None
        fts_db = tmp_path / "src/rag/store/lightrag_fts.db"
        assert fts_db.is_file()
        with sqlite3.connect(fts_db) as conn:
            assert conn.execute("SELECT count(*) FROM entities_fts").fetchone()[0] == 0
    finally:
        await rag.finalize_storages()


@pytest.mark.asyncio
async def test_fts_disabled_e2e(tmp_path, monkeypatch):
    """get_lightrag() with the opt-in off wires a pure-vector entity store."""
    import graph_rag.sherry_adapter as adapter
    from config.features import GRAPH_RAG_PATH

    monkeypatch.setattr(adapter, "SRC_DIR", tmp_path / "src")
    monkeypatch.setitem(GRAPH_RAG_PATH, "fts_hybrid_enabled", False)

    rag = await adapter.get_lightrag()
    try:
        assert rag.entities_vdb._fts is None
        assert rag.relationships_vdb._fts is None
        assert rag.chunks_vdb._fts is None
        assert not (tmp_path / "src/rag/store/lightrag_fts.db").exists()
    finally:
        await rag.finalize_storages()


# ---------------------------------------------------------------------------
# All-mode hermetic e2e (no LLM call; real SNKV storages + FTS shadow index)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_modes_e2e_with_fts(tmp_path):
    rag = _build_rag(tmp_path, attach_fts=True)
    await rag.initialize_storages()
    try:
        # Entity context is expanded from the graph store, so seed the graph
        # with the same names as the entity vector store.
        for name, data in _ENTITIES.items():
            await rag.chunk_entity_relation_graph.upsert_node(
                data["entity_name"],
                {
                    "entity_type": "concept",
                    "description": data["content"],
                    "source_id": data["source_id"],
                    "file_path": data["file_path"],
                },
            )
        await rag.chunk_entity_relation_graph.upsert_edge(
            "_strip_bom",
            "污点检测",
            {
                "description": "BOM helpers participate in taint cleaning",
                "keywords": "bom,taint",
                "weight": 1.0,
                "source_id": "chunk-a",
                "file_path": "src/util.py",
            },
        )

        await rag.entities_vdb.upsert(_ENTITIES)
        await rag.chunks_vdb.upsert(
            {
                "chunk-a": {
                    "content": "_strip_bom removes a UTF-8 BOM from a file",
                    "file_path": "src/util.py",
                    "tokens": 8,
                    "chunk_order_index": 0,
                },
                "chunk-b": {
                    "content": "污点检测 tracks tainted data flows",
                    "file_path": "src/security.py",
                    "tokens": 8,
                    "chunk_order_index": 1,
                },
            }
        )

        contexts = {}
        for mode in ("local", "global", "hybrid", "naive", "mix", "path"):
            param = QueryParam(
                mode=mode,
                only_need_context=True,
                ll_keywords=["_strip_bom", "污点检测"],
                hl_keywords=["_strip_bom", "污点检测"],
            )
            answer = await rag.aquery("Where is BOM handling implemented?", param=param)
            contexts[mode] = str(answer)
            assert contexts[mode], f"{mode} mode returned an empty context"
            assert "[Error]" not in contexts[mode], f"{mode} mode failed: {contexts[mode]}"

        # local/hybrid/mix/path all search entities_vdb and therefore see FTS.
        assert "_strip_bom" in contexts["local"]
        assert "污点检测" in contexts["hybrid"]
    finally:
        await rag.finalize_storages()
