"""``browser_press`` — one named key on the page."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, resolve_manager


class PressInput(BrowserPageInput):
    """Arguments of ``browser_press``."""

    keys: str = Field(
        description="One named key: Enter / Tab / Escape / Backspace / Delete / "
        "ArrowUp / ArrowDown / ArrowLeft / ArrowRight / PageUp / PageDown / Home / End / Space."
    )


class BrowserPressTool(BrowserTool):
    name: str = "browser_press"
    description: str = (
        "Press one named key on the page (Enter / Tab / Escape / arrows / "
        "PageUp / PageDown / Home / End / Backspace / Delete / Space)."
    )
    args_schema: type[BaseModel] = PressInput
    metadata: dict = MAIN_ONLY

    async def _arun(self, keys: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.press(session_id, keys, page)
            return f"pressed {outcome.get('pressed')} on page {outcome.get('page')}"

        return await guard(operation)
