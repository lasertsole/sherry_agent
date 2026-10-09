"""``browser_scroll`` — wheel-scroll the page."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserTool, SessionId, guard, resolve_manager


class ScrollInput(BaseModel):
    """Arguments of ``browser_scroll``."""

    delta_y: float = Field(default=0, description="Vertical wheel delta (positive scrolls down).")
    delta_x: float = Field(default=0, description="Horizontal wheel delta.")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class BrowserScrollTool(BrowserTool):
    name: str = "browser_scroll"
    description: str = (
        "Scroll the page by a wheel delta (positive delta_y scrolls down). Use "
        "small steps and re-snapshot to see the new content."
    )
    args_schema: type[BaseModel] = ScrollInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self,
        delta_y: float = 0,
        delta_x: float = 0,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.scroll(
                session_id, delta_y=delta_y, delta_x=delta_x, page_id=page
            )
            return f"scrolled {outcome.get('scrolled')} on page {outcome.get('page')}"

        return await guard(operation)
