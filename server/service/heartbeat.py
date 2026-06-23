import json
from typing import Any
from pathlib import Path
from loguru import logger
from models import main_llm
from config import PLUGINS_PATH
from tools import build_core_tools
from context_engine import assemble
from type.bus import OutboundMessage
from langchain.agents import create_agent
from channels import BaseChannel, channel_manager
from langgraph.graph.state import CompiledStateGraph
from workspace.prompt_builder import build_system_prompt
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage


async def process_heartbeat_task(task: str) -> str:
    try:
        # Get graph-memory system prompt
        assemble_result: dict[str, str] = await assemble(user_text=task, messages=[])
        graph_system_prompt_addition: str = assemble_result.get("system_prompt_addition", "")

        agent: CompiledStateGraph = create_agent(
            model=main_llm,
            tools=build_core_tools(),
        )

        messages: list[BaseMessage] = [
            SystemMessage(
                content=
                build_system_prompt(selected_file_names=[])
                + graph_system_prompt_addition
            ),
            HumanMessage(content=task)
        ]
        result: dict[str, Any] = agent.invoke(input={"messages": messages})
        res_messages = result["messages"]

        return res_messages[-1].content
    except Exception as e:
        logger.exception(e)
        return f"Error occurred: {e}"

async def process_heartbeat_notify(agent_res: str) -> None:
    channels_json: Path = PLUGINS_PATH / "channels/config.json"
    res: dict[str, str] = {}

    if channels_json.exists():
        channels_configs: dict[str, Any] = json.loads(channels_json.read_text())
        for name, config in channels_configs.items():
            if config.get("heartbeat", False) and config.get("receiver", False):
                res[name] = config["receiver"]

    for name, receiver in res.items():
        channel: BaseChannel = channel_manager.get_channel(name)
        if channel:
            await channel.send(OutboundMessage(channel=name, chat_id = receiver, content = agent_res))