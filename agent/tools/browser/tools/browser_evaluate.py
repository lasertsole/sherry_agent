"""``browser_evaluate`` — one JavaScript expression, opt-in via ``allow_evaluate``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserTool, SessionId, guard, resolve_manager


class EvaluateInput(BaseModel):
    """Arguments of ``browser_evaluate``."""

    expression: str = Field(description="JavaScript expression (JSON-serializable result).")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class BrowserEvaluateTool(BrowserTool):
    name: str = "browser_evaluate"
    description: str = (
        "Run one JavaScript expression in the page and get its JSON value back. "
        "The escape hatch for what the typed tools cannot express (reading "
        "computed data, scrolling inside a container, extracting a table). The "
        "page is live: an expression can change it."
    )
    args_schema: type[BaseModel] = EvaluateInput
    metadata: dict = MAIN_ONLY

    async def _arun(self, expression: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.evaluate(session_id, expression, page)
            tail = " (truncated)" if outcome.get("truncated") else ""
            return f"page {outcome.get('page')} result{tail}: {outcome.get('result')}"

        return await guard(operation)
