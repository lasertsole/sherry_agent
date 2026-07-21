from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langgraph.graph.state import CompiledStateGraph
from models import build_main_llm, build_auxiliary_llm
from agent.checkpointer import build_async_sqlite_checkpointer
from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from agent.tools import memory_store, build_main_tools, build_subagent_tool
from .checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver
from .middlewares import (Summarization, ToolCallNormalize, MultimodalProcessor, ContextEngineHook, ToolGuardrails,
                          IterationBudget, HeartbeatStaleness)
from .smart_tool_node import patch_tool_node

# # 只有幂等的工具才能并行执行，非幂等串行执行
# patch_tool_node()

# ── Extended state schema ────────────────────────────────────────────────
# Carries ``session_id`` through the graph so that middlewares reading
# ``request.state["session_id"]`` is used by middlewares that need it

class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""
    session_id: str

# Load memory markdown files from disk; keep them unchanged until
# compression is triggered during this server run.
memory_store.load_from_disk()

# Build tool list
_tools: list[BaseTool] = build_main_tools()
subagent_tool = build_subagent_tool()
_tools.append(subagent_tool)

def get_agent_tools()-> list[BaseTool]:
    return _tools

_agent: CompiledStateGraph | None = None
async def built_agent(
    temperature: float = 0.8,
)-> CompiledStateGraph:
    global _agent
    if _agent is None:
        checkpointer: ThreadSafeAsyncSqliteSaver = await build_async_sqlite_checkpointer()

        # create table before using
        await checkpointer.setup()

        # Delete all checkpoints but keeps the latest checkpoint
        await checkpointer.aclean_old_checkpoints()

        main_llm = build_main_llm()
        auxiliary_llm = build_auxiliary_llm()

        # Build the agent
        _agent =  create_agent(
            model = main_llm.bind(temperature=temperature),
            state_schema = StateSchema,
            checkpointer = checkpointer,
            tools = get_agent_tools(),
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                ToolCallNormalize(),
                HeartbeatStaleness(),
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    trigger=[
                        ("tokens", int(main_llm_max_tokens / 2))
                    ],
                    keep=("messages", 10),

                ),
            ],
        )

    return _agent