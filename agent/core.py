from skills import build_skills_snapshot
from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from models import main_llm, auxiliary_llm
from langchain.agents.middleware import AgentState
from langgraph.graph.state import CompiledStateGraph
from agent.checkpointer import build_async_sqlite_checkpointer
from agent.tools import memory_store, build_main_tools, build_subagent_tool
from .checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver
from .middlewares import Summarization, ToolCallNormalize, MultimodalProcessor, ToolTimeout, ContextEngineHook, ToolGuardrails, IterationBudget


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
                ToolTimeout(),
                Summarization(
                    model=auxiliary_llm,
                    trigger=[
                        ("fraction", 0.5),
                        ("messages", 40),
                        ("tokens", 30000)
                    ],
                    keep=("messages", 10),

                ),
            ],
        )

    return _agent