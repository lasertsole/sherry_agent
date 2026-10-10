"""``browser_screenshot`` — capture a PNG on disk and return it to the model.

The capture ALWAYS lands on disk and the text names its path. When the serving
model reads images natively the image itself travels with the result
(``native_image_content``): looking at a page must not require the
``image_to_text`` skill's local vision model. A model that cannot read images
keeps the text + path answer, and the media pipeline strips the block again.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, resolve_manager


class ScreenshotInput(BrowserPageInput):
    """Arguments of ``browser_screenshot``."""

    full_page: bool = Field(default=False, description="Capture beyond the viewport (whole page).")


class BrowserScreenshotTool(BrowserTool):
    name: str = "browser_screenshot"
    description: str = (
        "Capture the page (or, with full_page=true, the whole scrollable page) "
        "as a PNG. The image is returned for you to look at directly when the "
        "model supports vision; otherwise the result names the saved path and "
        "the image_to_text skill reads it. Prefer browser_snapshot for text."
    )
    args_schema: type[BaseModel] = ScreenshotInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self, full_page: bool = False, page: str | None = None, session_id: str = ""
    ) -> str | list[dict[str, Any]]:
        async def operation() -> str | list[dict[str, Any]]:
            manager = resolve_manager()
            outcome = await manager.screenshot(session_id, full_page=full_page, page_id=page)
            size = (
                f"{outcome.get('width')}×{outcome.get('height')}"
                if outcome.get("width")
                else f"{outcome.get('bytes')} bytes"
            )
            text = f"screenshot saved: {outcome.get('path')} ({size})."
            # Native vision when the model has it; the text is the skill path's
            # input either way, so it is never dropped.
            from agent.middlewares.media_pipeline.tool_media import native_image_content

            return native_image_content(session_id, text, str(outcome.get("path") or ""))

        return await guard(operation)
