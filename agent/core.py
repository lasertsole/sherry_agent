from enum import Enum
from pydantic import BaseModel
from skills import build_skills_snapshot
from langgraph.types import Checkpointer
from langchain.agents import create_agent
from tools import build_all_tools, memory_store
from context_engine import add_session_if_not_exists
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import InMemorySaver
from models import chat_model, reasoner_model, vl_model
from .middlewares import ContextEngineHook, Summarization, ToolLoopPrevention

# 服务器启动时重构技能快照，用于保证本次服务器运行中skills提示词稳定，从而保证模型 前缀缓存 稳定
build_skills_snapshot()

# 加载memory文件夹下的md文件，并在保证本次服务器运行触发压缩前保持不变
memory_store.load_from_disk()

class ModelType(Enum):
    """字符串枚举，可以直接与字符串比较"""
    CHAT_MODEL = chat_model
    REASONER_MODEL = reasoner_model
    VL_MODEL = vl_model

def built_agent(
    session_id: str,
    system_prompt: str | None = None,
    model_type:ModelType = ModelType.CHAT_MODEL,
    temperature: float = 0.8,
    enable_tool: bool = True,
    checkpointer: Checkpointer | None = None,
    response_format: BaseModel | None = None
)-> CompiledStateGraph:
    # 若无会话记录 则创建 session记录
    add_session_if_not_exists(session_id)

    model = model_type.value.bind(temperature=temperature)

    if checkpointer is None:
        checkpointer = InMemorySaver()

    #生成agent对象
    agent = create_agent(
        model = model,
        checkpointer = checkpointer,
        system_prompt = system_prompt,
        tools = build_all_tools(session_id) if enable_tool else None,
        middleware = [
            ContextEngineHook(session_id=session_id),
            Summarization(
                model=chat_model,
                session_id=session_id,
                trigger=[
                    ("fraction", 0.5),
                    ("messages", 40),
                    ("tokens", 30000)
                ],
                keep=("messages", 10),

            ),
            ToolLoopPrevention(session_id=session_id),
        ],
        response_format = response_format
    )

    return agent