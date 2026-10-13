"""``browser_type`` — type text into an input, optionally submitting."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, resolve_manager


class TypeInput(BrowserPageInput):
    """Arguments of ``browser_type``."""

    text: str = Field(
        description="Text to type into the focused element (or the one named by ref)."
    )
    ref: str | None = Field(
        default=None,
        description="Input ref from a snapshot; omit to type into whatever is focused.",
    )
    submit: bool = Field(default=False, description="Press Enter after typing.")


class BrowserTypeTool(BrowserTool):
    name: str = "browser_type"
    description: str = (
        "Type text into an input: pass a snapshot ref (recommended) or omit ref "
        "to type into the currently focused element. Set submit=true to press "
        "Enter afterwards (e.g. to run a search)."
    )
    args_schema: type[BaseModel] = TypeInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self,
        text: str,
        ref: str | None = None,
        submit: bool = False,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.type_text(
                session_id, text, ref=ref, submit=submit, page_id=page
            )
            tail = " and pressed Enter" if outcome.get("submitted") else ""
            return f"typed {outcome.get('typed')} character(s){tail} on page {outcome.get('page')}"

        return await guard(operation)
