"""``browser_console`` — read buffered console output and JS exceptions."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, page_line, resolve_manager


class ConsoleInput(BrowserPageInput):
    """Arguments of ``browser_console``."""

    level: str = Field(
        "all",
        description="Filter by level: all / error / warn / info / debug. Exceptions count as error.",
    )
    include_exceptions: bool = Field(
        True, description="Include uncaught JS exceptions (with their stack)."
    )
    limit: int = Field(100, ge=1, le=200, description="Newest events to return.")


class BrowserConsoleTool(BrowserTool):
    name: str = "browser_console"
    description: str = (
        "Read the console messages and uncaught JS exceptions captured on the "
        "session's page since it was opened or last navigated. Capture starts "
        "automatically with the page (SHERRY_BROWSER_DEBUG_CAPTURE=1). Newest "
        "last. Use it after a reload to see what the frontend is complaining about."
    )
    args_schema: type[BaseModel] = ConsoleInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self,
        level: str = "all",
        include_exceptions: bool = True,
        limit: int = 100,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = manager.console_events(session_id, page, level=level, limit=limit)
            if not outcome.get("enabled"):
                return (
                    "browser console capture is disabled "
                    "(SHERRY_BROWSER_DEBUG_CAPTURE=0); navigate after enabling it."
                )
            events = outcome.get("events") or []
            if not include_exceptions:
                events = [event for event in events if event.get("kind") != "exception"]
            header = (
                f"{page_line(outcome)} — console: {len(events)} of "
                f"{outcome.get('buffered', 0)} buffered event(s)"
            )
            if not events:
                return f"{header}\n(no console output captured)"
            lines = [header]
            for event in events:
                text = str(event.get("text") or "").replace("\n", " ")[:600]
                location = ""
                if event.get("url"):
                    location = f" ({event['url']}:{event.get('line', 0)}:{event.get('column', 0)})"
                lines.append(f"[{event.get('level') or 'info'}] {text}{location}")
                for frame in event.get("stack") or []:
                    lines.append(
                        "    at "
                        f"{frame.get('function') or '<anonymous>'} "
                        f"({frame.get('url')}:{frame.get('line')}:{frame.get('column')})"
                    )
            return "\n".join(lines)

        return await guard(operation)
