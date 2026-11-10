"""``browser_navigate`` — open a URL in the session's page."""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel, Field

from ._shared import (
    MAIN_ONLY,
    BrowserPageInput,
    BrowserTool,
    guard,
    page_line,
    resolve_manager,
    validate_navigation_url,
    validate_page_read_url,
)


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
        "can watch and take over in the 工具箱·浏览器 panel. Public http(s) URLs "
        "only; local/private targets need SHERRY_BROWSER_ALLOW_LOCAL=1."
    )
    args_schema: type[BaseModel] = NavigateInput
    metadata: dict = MAIN_ONLY

    async def _arun(self, url: str, page: str | None = None, session_id: str = "") -> str:
        blocked = await validate_navigation_url(url)
        if blocked is not None:
            return blocked

        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.navigate(session_id, url, page)
            final_url = str(outcome.get("url") or url)
            if final_url != url:
                redirected = await validate_page_read_url(final_url)
                if redirected is not None:
                    # A public URL redirected to a blocked target: the page
                    # already fetched it. Park the page before anything can
                    # snapshot the content.
                    try:
                        await manager.navigate(session_id, "about:blank", page)
                    except Exception:  # noqa: BLE001 - the refusal is what matters
                        logger.debug("browser: failed to park a blocked page", exc_info=True)
                    detail = redirected.removeprefix("Error: ")
                    return f"Error: navigation redirected to a blocked URL: {detail}"
            state = "loaded" if outcome.get("loaded") else "still loading (timed out waiting)"
            return f"{page_line(outcome)} — {state}"

        return await guard(operation)
