from typing import Any, cast
from .string_to_int import rand_str_to_int
from langchain_core.runnables import RunnableConfig


def build_agent_config(session_id: str, args: list[dict[str, Any]] | None = None) -> RunnableConfig:
    merged: dict[str, Any] = {"configurable": {"thread_id": rand_str_to_int(session_id)}}

    # Merge additional arguments into the config
    if args is not None:
        for arg in args:
            merged.update(arg)

    return cast("RunnableConfig", merged)
