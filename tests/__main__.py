"""
使用 codeact_lib 实现的 LangGraph CodeAct agent 测试。

该库复制自 https://github.com/langchain-ai/langgraph-codeact（已归档），
实现了 CodeAct 架构（https://arxiv.org/abs/2402.01030），
作为 JSON function-calling 的替代方案。
"""

import sys
from pathlib import Path
from pub_func import extract_final_answer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import build_main_tools
from models import main_llm
from agent import codeact_agent
from pub_func import build_agent_config
from agent.middlewares import IterationBudget

if __name__ == "__main__":
    agent = codeact_agent(model=main_llm, tools=build_main_tools(), middleware= [IterationBudget()])

    for m in agent.stream(
        {"messages": [{"role": "user", "content": "俄乌伤亡对比"}]},
        config=build_agent_config("main"),
    ):
        print(m)

    # print(extract_final_answer(result))