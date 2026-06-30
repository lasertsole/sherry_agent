from loguru import logger
from models import main_llm
from skills import build_skills_snapshot
from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langgraph.graph.state import CompiledStateGraph
from workspace.prompt_builder import build_system_prompt
from agent.checkpointer import build_async_sqlite_checkpointer
from tools import memory_store, build_main_tools, build_subagent_tool
from .checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver
from .middlewares import Summarization, ToolLoopPrevention, ToolCallNormalize, MultimodalProcessor, ToolTimeout, ContextEngineHook


# ── Extended state schema ────────────────────────────────────────────────
# Carries ``session_id`` through the graph so that middlewares reading
# ``request.state["session_id"]`` is used by middlewares that need it

class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""
    session_id: str

# Rebuild skill snapshot at server start to keep skills prompt stable
# throughout this server run, ensuring reliable model prefix caching.
build_skills_snapshot()

# Load memory markdown files from disk; keep them unchanged until
# compression is triggered during this server run.
memory_store.load_from_disk()


_agent: CompiledStateGraph | None = None
async def built_agent(
    temperature: float = 0.8,
)-> CompiledStateGraph:
    global _agent
    if _agent is None:
        model = main_llm.bind(temperature=temperature)
        checkpointer: ThreadSafeAsyncSqliteSaver = await build_async_sqlite_checkpointer()

        # Delete all checkpoints but keeps the latest checkpoint
        await checkpointer.aclean_old_checkpoints()

        # Build tool list
        tools: list[BaseTool] = build_main_tools()
        subagent_tool = build_subagent_tool()
        tools.append(subagent_tool)
        tool_count = len(tools)
        logger.debug(f"Tools built: tool_count={tool_count}")

        # Build the agent
        _agent =  create_agent(
            model = model,
            state_schema = StateSchema,
            checkpointer = checkpointer,
            tools = tools,
            system_prompt= build_system_prompt(),
            middleware = [
                ContextEngineHook(),
                MultimodalProcessor(),
                Summarization(
                    model=main_llm,
                    trigger=[
                        ("fraction", 0.5),
                        ("messages", 40),
                        ("tokens", 30000)
                    ],
                    keep=("messages", 10),

                ),
                ToolLoopPrevention(),
                ToolCallNormalize(),
                ToolTimeout()
            ],
        )

    return _agent