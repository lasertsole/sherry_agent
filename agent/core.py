import time

from langchain_core.tools import BaseTool
from loguru import logger
from models import main_llm
from pydantic import BaseModel
from skills import build_skills_snapshot
from langgraph.types import Checkpointer
from langchain.agents import create_agent
from context_engine import add_session_if_not_exists
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import InMemorySaver
from tools import memory_store, build_main_tools, build_subagent_tool
from .middlewares import (ContextEngineHook, Summarization, ToolLoopPrevention, ToolCallNormalize, MultimodalProcessor,
                          ToolTimeout)

# Rebuild skill snapshot at server start to keep skills prompt stable
# throughout this server run, ensuring reliable model prefix caching.
build_skills_snapshot()

# Load memory markdown files from disk; keep them unchanged until
# compression is triggered during this server run.
memory_store.load_from_disk()

def built_agent(
    session_id: str,
    system_prompt: str,
    temperature: float = 0.8,
    checkpointer: Checkpointer | None = None,
    response_format: BaseModel | None = None
)-> CompiledStateGraph:
    start_time = time.time()
    
    # Create a session record if one does not already exist
    add_session_if_not_exists(session_id)
    
    logger.info(
        f"Building agent: session_id={session_id}"
    )

    model = main_llm.bind(temperature=temperature)

    if checkpointer is None:
        checkpointer = InMemorySaver()

    # Build tool list
    tools: list[BaseTool] = build_main_tools(session_id)
    subagent_tool = build_subagent_tool(session_id)
    tools.append(subagent_tool)
    tool_count = len(tools)
    logger.debug(f"Tools built: session_id={session_id}, tool_count={tool_count}")

    # Build the agent
    agent = create_agent(
        model = model,
        checkpointer = checkpointer,
        system_prompt = system_prompt,
        tools = tools,
        middleware = [
            MultimodalProcessor(session_id=session_id),
            ContextEngineHook(session_id=session_id),
            Summarization(
                model=main_llm,
                session_id=session_id,
                trigger=[
                    ("fraction", 0.5),
                    ("messages", 40),
                    ("tokens", 30000)
                ],
                keep=("messages", 10),

            ),
            ToolLoopPrevention(session_id=session_id),
            ToolCallNormalize(session_id=session_id),
            ToolTimeout(session_id=session_id)
        ],
        response_format = response_format
    )

    return agent