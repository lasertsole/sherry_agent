"""``browser_click`` — click a snapshot ref or a viewport coordinate."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserTool, SessionId, guard, resolve_manager


class ClickInput(BaseModel):
    """Arguments of ``browser_click``."""

    ref: str | None = Field(default=None, description='Element ref from a snapshot (e.g. "e5").')
    x: float | None = Field(
        default=None, description="Viewport x, when clicking by coordinate instead of ref."
    )
    y: float | None = Field(
        default=None, description="Viewport y, when clicking by coordinate instead of ref."
    )
    button: str = Field(default="left", description="Mouse button: left / middle / right.")
    double: bool = Field(default=False, description="Double click.")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class BrowserClickTool(BrowserTool):
    name: str = "browser_click"
    description: str = (
        'Click an element by its snapshot ref (e.g. "e5"), or by viewport x/y '
        "coordinates. Returns what was clicked. Re-snapshot before the next "
        "action when the page changed."
    )
    args_schema: type[BaseModel] = ClickInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.click(
                session_id, ref=ref, x=x, y=y, button=button, double=double, page_id=page
            )
            label = outcome.get("text") or outcome.get("tag") or ""
            suffix = f" («{label}»)" if label else ""
            return f"clicked [{outcome.get('clicked')}]{suffix} on page {outcome.get('page')}"

        return await guard(operation)
