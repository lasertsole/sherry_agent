from typing import Any
from typing_extensions import override
from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware


class CommanderSummarization(SummarizationMiddleware):
    """Summarization middleware for commander (pass-through for now)."""

    @override
    def before_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        return super().before_model(state, runtime)

    @override
    async def abefore_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        return await super().abefore_model(state, runtime)
