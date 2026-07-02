from typing import Any
from .rand_str_to_int import rand_str_to_int
from langchain_core.runnables import RunnableConfig

def build_agent_config(session_id: str, args: list[dict[str, Any]] | None = None) -> RunnableConfig:
    try:
        config: RunnableConfig = {"configurable": {"thread_id": rand_str_to_int(session_id)}}

        # Merge additional arguments into the config
        if args is not None:
            for arg in args:
                config.update(arg)

        return config
    except ValueError:
        raise Exception("session_id must be an integer")