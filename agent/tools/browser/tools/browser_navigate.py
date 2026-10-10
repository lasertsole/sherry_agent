"""``browser_navigate`` — open a URL in the session's page."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, page_line, resolve_manager


class NavigateInput(BrowserPageInput):
    """Arguments of ``browser_navigate``."""

    url: str = Field(description="Absolute URL to open (https://… or http://…).")


class BrowserNavigateTool(BrowserTool):
    name: str = "browser_navigate"
    description: str = (
        "Open a URL in the session's browser page (the page is created on first "
        "use) and wait for it to load. Returns the final URL, title and whether "
        "the load finished. Read the page with browser_snapshot afterwards — "
        "snapshot refs die on navigation. The browser is a real page a person "
        "can watch and take over in the 工具箱·浏览器 panel."
    )
    args_schema: type[BaseModel] = NavigateInput
    metadata: dict = MAIN_ONLY

    async def _arun(self, url: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.navigate(session_id, url, page)
            state = "loaded" if outcome.get("loaded") else "still loading (timed out waiting)"
            return f"{page_line(outcome)} — {state}"

        return await guard(operation)
