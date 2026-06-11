import os
import sys
import numpy as np

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.path import SRC_DIR
from lightrag.utils import EmbeddingFunc
from lightrag import LightRAG, QueryParam
from models import embed_model, simple_chat_model
from models.reranker_model import reranker_model
from langchain_core.messages import SystemMessage, HumanMessage

# 设置 LightRAG 知识图谱大小控制的环境变量，缓解图谱无限膨胀
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
    """将本地 simple_chat_model 适配为 LightRAG 需要的格式"""
    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    if history_messages:
        messages.extend(history_messages)
    messages.append(HumanMessage(content=prompt))

    response = await simple_chat_model.ainvoke(messages)
    return response.content

async def _embedding_func(texts: list[str]) -> np.ndarray:
    """将本地 embed_model 适配为 LightRAG 需要的格式"""
    embeddings = embed_model.embed_documents(texts)
    return np.array(embeddings)


async def _rerank_model_func(
    query: str, documents: list[str], top_n: int | None = None
) -> list[dict]:
    """将本地 RerankerModel 适配为 LightRAG rerank_model_func 格式

    LightRAG apply_rerank_if_enabled expects:
    New format: [{"index": int, "relevance_score": float}, ...]
    """
    results = reranker_model.rank(query=query, documents=documents, top_k=top_n)
    return [
        {
            "index": r["corpus_id"],
            "relevance_score": r["score"],
        }
        for r in results
    ]

_lightRAG: LightRAG | None = None


async def get_lightrag() -> LightRAG:
    """获取 LightRAG 单例实例"""
    working_dir: str = (SRC_DIR / "rag" / "store").resolve().as_posix()

    global _lightRAG

    if _lightRAG is None:
        _lightRAG = LightRAG(
            working_dir=working_dir,
            llm_model_func=_llm_model_func,
            embedding_func=EmbeddingFunc(
                embedding_dim=1024,  # BGE-M3 模型的维度
                max_token_size=8192,
                func=_embedding_func,
            ),
            rerank_model_func=_rerank_model_func,
        )

        await _lightRAG.initialize_storages()

    return _lightRAG