"""``browser_snapshot`` — the page's readable text plus ref-addressable elements."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ._shared import (
    MAIN_ONLY,
    BrowserPageInput,
    BrowserTool,
    guard,
    page_line,
    resolve_manager,
    withhold_blocked_page,
)


class SnapshotInput(BrowserPageInput):
    """Arguments of ``browser_snapshot``."""

    max_elements: int | None = Field(
        default=None, ge=1, le=1000, description="Cap on interactive elements (default 200)."
    )


def _format_snapshot(outcome: dict[str, Any]) -> str:
    """Snapshot → readable text: the page text, then the ref-addressable elements."""
    lines = [page_line(outcome)]
    text = str(outcome.get("text") or "").strip()
    lines.append("page text:" if text else "page text: (empty)")
    if text:
        lines.append(text)
    elements = outcome.get("elements") or []
    if elements:
        lines.append("")
        lines.append(
            f"interactive elements ({len(elements)}; refs die on navigation — "
            "re-snapshot after any page change):"
        )
        for element in elements:
            parts = [f"  [{element.get('ref')}] {element.get('tag')}"]
            if element.get("type"):
                parts.append(f"type={element['type']}")
            name = element.get("name")
            if name:
                parts.append(f"«{name}»")
            if element.get("value"):
                parts.append(f"value=«{element['value']}»")
            if element.get("href"):
                parts.append(f"href={element['href']}")
            if element.get("checked"):
                parts.append("checked")
            lines.append(" ".join(parts))
    if outcome.get("truncated"):
        lines.append("… (element list truncated; use max_elements or browser_evaluate)")
    return "\n".join(lines)


class BrowserSnapshotTool(BrowserTool):
    name: str = "browser_snapshot"
    description: str = (
        "Read the page: its visible text plus the interactive elements (buttons, "
        "links, inputs) numbered as refs like [e5]. Pass a ref to browser_click / "
        "browser_type. Prefer this over browser_screenshot — text is cheaper and "
        "sharper; screenshots are for layout/visual questions only."
    )
    args_schema: type[BaseModel] = SnapshotInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self, page: str | None = None, max_elements: int | None = None, session_id: str = ""
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = await manager.snapshot(session_id, page, max_elements=max_elements)
            # Re-check where the page actually is (a click/redirect/script may
            # have moved it) before any text leaves this function.
            withheld = await withhold_blocked_page(
                manager, session_id, page, str(outcome.get("url") or "")
            )
            if withheld is not None:
                return withheld
            return _format_snapshot(outcome)

        return await guard(operation)
