import os
import sys
import numpy as np

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from graph_rag.vendored_lightrag import LightRAG
from config.path import SRC_DIR
from graph_rag.vendored_lightrag.utils import EmbeddingFunc
from langchain_core.messages import SystemMessage, HumanMessage

# Set environment variables that control LightRAG knowledge-graph size to curb unbounded graph growth
os.environ.setdefault("MAX_SOURCE_IDS_PER_ENTITY", "50")
os.environ.setdefault("MAX_SOURCE_IDS_PER_RELATION", "50")
os.environ.setdefault("SOURCE_IDS_LIMIT_METHOD", "FIFO")
os.environ.setdefault("RELATED_CHUNK_NUMBER", "5")


async def _llm_model_func(
    prompt: str,
    system_prompt: str = None,
    history_messages: list = None,
    **kwargs,
) -> str:
    """将本地 auxiliary_llm 适配为 LightRAG 需要的格式"""
    from models import build_auxiliary_llm

    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    if history_messages:
        messages.extend(history_messages)
    messages.append(HumanMessage(content=prompt))

    auxiliary_llm = build_auxiliary_llm()
    response = await auxiliary_llm.ainvoke(messages)
    return response.content


async def _embedding_func(texts: list[str]) -> np.ndarray:
    """将本地 embed_model 适配为 LightRAG 需要的格式"""
    from models import build_embed_model

    embed_model = build_embed_model()
    embeddings = embed_model.embed_documents(texts)
    return np.array(embeddings)


async def _rerank_model_func(
    query: str, documents: list[str], top_n: int | None = None
) -> list[dict]:
    """将本地 RerankerModel 适配为 LightRAG rerank_model_func 格式

    LightRAG apply_rerank_if_enabled expects:
    New format: [{"index": int, "relevance_score": float}, ...]
    """
    from models.reranker_model import reranker_model

    results = reranker_model.rank(query=query, documents=documents, top_k=top_n)
    return [
        {
            "index": r["corpus_id"],
            "relevance_score": r["score"],
        }
        for r in results
    ]


async def get_lightrag() -> LightRAG:
    """获取 LightRAG 单例实例"""
    working_dir: str = (SRC_DIR / "rag" / "store").resolve().as_posix()

    # SNKV storage backends are native to the vendored LightRAG (registered
    # directly in ``kg/__init__.py``), so no runtime registration is needed.
    _lightRAG = LightRAG(
        working_dir=working_dir,
        llm_model_func=_llm_model_func,
        embedding_func=EmbeddingFunc(
            embedding_dim=1024,  # Dimension of the BGE-M3 model
            max_token_size=8192,
            func=_embedding_func,
        ),
        rerank_model_func=_rerank_model_func,
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
    )

    await _lightRAG.initialize_storages()

    return _lightRAG
