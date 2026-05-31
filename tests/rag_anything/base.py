import os
import sys
import asyncio
import numpy as np
from logging import getLogger

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import SRC_DIR
from lightrag.utils import EmbeddingFunc
from lightrag import LightRAG, QueryParam
from models import embed_model, simple_chat_model, vl_model
from langchain_core.messages import SystemMessage, HumanMessage

# 设置 LightRAG 知识图谱大小控制的环境变量， 缓解图谱无限膨胀
os.environ.setdefault("MAX_SOURCE_IDS_PER_ENTITY", "50")
os.environ.setdefault("MAX_SOURCE_IDS_PER_RELATION", "50")
os.environ.setdefault("SOURCE_IDS_LIMIT_METHOD", "FIFO")
os.environ.setdefault("RELATED_CHUNK_NUMBER", "5")

logger = getLogger(__name__)

async def _local_llm_func(prompt: str, system_prompt: str = None, history_messages: list = None, **kwargs) -> str:
    """将本地 simple_chat_model 适配为 LightRAG 需要的格式"""

    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    if history_messages:
        messages.extend(history_messages)
    messages.append(HumanMessage(content=prompt))

    # 调用你项目里的聊天模型
    response = await simple_chat_model.ainvoke(messages)
    return response.content


async def _local_embedding_func(texts: list[str]) -> np.ndarray:
    """将本地 embed_model 适配为 LightRAG 需要的格式"""
    # 调用你项目里的嵌入模型
    embeddings = embed_model.embed_documents(texts)
    return np.array(embeddings)


_lightRAG: LightRAG | None = None
async def get_lightrag() -> LightRAG:
    working_dir: str = (SRC_DIR / "rag" / "lightrag_db").resolve().as_posix()

    global _lightRAG

    if _lightRAG is None:
        _lightRAG = LightRAG(
            working_dir=working_dir,
            llm_model_func=_local_llm_func,
            embedding_func=EmbeddingFunc(
                embedding_dim=1024,  # BGE-M3 模型的维度
                max_token_size=8192,
                func=_local_embedding_func
            ),
        )
        await _lightRAG.initialize_storages()

    return _lightRAG


async def add_rag(session_id: str, histories: list[str]) -> None:
    """增加lightrag数据"""

    lightrag = await get_lightrag()
    logger.info(f"Session {session_id}: 调用 lightrag.ainsert...")
    try:
        res = await asyncio.wait_for(
            lightrag.ainsert(histories),
            timeout=60.0 * 15  # 15分钟超时
        )
        logger.info(f"Session {session_id}: ✅ 插入成功")
    except asyncio.TimeoutError:
        logger.error(f"Session {session_id}: ❌ 插入超时(15min)! LightRAG 可能卡死了!")
        raise RuntimeError(f"LightRAG ainsert timeout for session {session_id}")


async def retrieve_rag(session_id: str, query_text: str) -> str:
    """召回lightrag数据"""

    lightrag = await get_lightrag()

    res = await lightrag.aquery(
        query_text,
        param=QueryParam(
            mode="local",  # 默认使用本地模式,确保快速召回
            only_need_context=True,  # 直接回复召回的关系，不经过llm总结
            top_k=5,
            chunk_top_k=3,
            max_entity_tokens=1000,
            max_relation_tokens=1000,
            max_total_tokens=3000,
            enable_rerank=False,
        )
    )

    return res


async def delete_rag(session_id: str, entity_names: list[str]) -> None:
    """物理删除节点和相关边，而不是逻辑删除"""
    lightrag = await get_lightrag()

    for entity_name in entity_names:
        try:
            lightrag.delete_by_entity(entity_name)
            print(f"✅ 已删除实体: {entity_name}")
        except Exception as e:
            print(f"❌ 删除失败 {entity_name}: {e}")