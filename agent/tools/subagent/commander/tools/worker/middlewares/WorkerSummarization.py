from typing import Any
from loguru import logger
from typing_extensions import override
from langchain.agents import AgentState
from langchain_core.messages import AnyMessage, HumanMessage
from langchain.agents.middleware import SummarizationMiddleware

_GOAL_DELIMITER = "[ORIGINAL TASK — this is your primary objective; always keep it in mind]"
_SUMMARY_DELIMITER = "[COMPACTION SUMMARY — progress made so far]"


class WorkerSummarization(SummarizationMiddleware):
    """Summarization middleware for worker subagents that preserves the original task.

    The first HumanMessage (the worker's task) is captured before any compression.
    After each compression the original task text is prepended to the summary
    HumanMessage so the worker never loses sight of its goal, even across
    multiple compaction rounds.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._original_goal: str | None = None

    def _capture_goal(self, messages: list[AnyMessage]) -> None:
        if self._original_goal is not None:
            return
        for m in messages:
            if isinstance(m, HumanMessage) and not getattr(m, "additional_kwargs", {}).get("lc_source"):
                content = m.content if isinstance(m.content, str) else str(m.content)
                if content.strip():
                    self._original_goal = content.strip()
                    logger.debug("WorkerSummarization: captured original goal ({} chars)", len(self._original_goal))
                    return

    @override
    def before_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        logger.debug("{} before_model hook fired", type(self).__name__)
        self._capture_goal(state.get("messages", []))
        logger.debug("WorkerSummarization.before_model: session_id={}, role={}",
                     state.get("session_id"), state.get("role"))
        res = super().before_model(state, runtime)
        if res is not None and self._original_goal:
            self._inject_goal(res)
        return res

    @override
    async def abefore_model(self, state: AgentState[Any], runtime) -> dict[str, Any] | None:
        logger.debug("{} abefore_model hook fired", type(self).__name__)
        self._capture_goal(state.get("messages", []))
        logger.debug("WorkerSummarization.abefore_model: session_id={}, role={}",
                     state.get("session_id"), state.get("role"))
        res = await super().abefore_model(state, runtime)
        if res is not None and self._original_goal:
            self._inject_goal(res)
        return res

    def _inject_goal(self, result: dict[str, Any]) -> None:
        messages = result.get("messages", [])
        for i, m in enumerate(messages):
            if not isinstance(m, HumanMessage):
                continue
            if not getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization":
                continue
            summary_text = m.content if isinstance(m.content, str) else str(m.content)
            prefix = f"{_GOAL_DELIMITER}\n\n{self._original_goal}\n\n{_SUMMARY_DELIMITER}\n\n"
            if summary_text.startswith(prefix):
                return
            messages[i] = HumanMessage(
                content=f"{prefix}{summary_text}",
                additional_kwargs=dict(m.additional_kwargs) if m.additional_kwargs else {},
                id=m.id,
            )
            return
