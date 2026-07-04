from .utils import eval_sandbox
from .core import create_codeact
from typing import Any, Callable, Sequence
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel
from langchain.agents.middleware.types import AgentMiddleware

def codeact_agent(
    model: BaseChatModel,
    tools: Sequence[StructuredTool],
    system_prompt: str | None = None,
    eval_fn: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]] | None = None,
    *,
    response_format: type[BaseModel] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
):
    """构建 CodeAct Agent（session 级，只构建一次）。

    Args:
        model: 用于生成代码的语言模型
        tools: agent 可用的工具列表
        system_prompt: 可选的自定义系统提示词
        eval_fn: 在沙箱中执行代码的函数。为 None 则使用内置 eval_sandbox。
        response_format: 可选的 Pydantic 模型，提供时 LLM 输出 JSON 代码块
            会解析为结构化响应并存储在 structured_response 中。
        middleware: 可选的 middleware 列表，用于拦截/增强 agent 执行。
    """
    _eval_fn = eval_fn or eval_sandbox

    code_act = create_codeact(
        model=model,
        tools=tools,
        eval_fn=_eval_fn,
        system_prompt=system_prompt,
        response_format=response_format,
        middleware=middleware,
    )
    return code_act.compile(checkpointer=MemorySaver())

__all__ = [
    "codeact_agent"
]