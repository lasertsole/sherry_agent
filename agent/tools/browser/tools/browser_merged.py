"""The merged browser surface: one ``browser`` tool over seven verbs.

The eight ``browser_*`` tools are thin shells over the same ``BrowserManager``.
The model now sees one ``browser`` tool for the seven everyday verbs
(``navigate`` / ``snapshot`` / ``click`` / ``type`` / ``press`` / ``scroll`` /
``screenshot``); ``browser_evaluate`` stays a SEPARATE, opt-in tool because it
registers only under ``allow_evaluate`` and must keep disappearing entirely
while the escape hatch is off.

The original per-verb classes remain the handlers (their text formatting and
``guard`` error contract are the behavior under test); this module only
validates the action-shaped arguments and delegates. A handler is called with
exactly the parameters its own ``_arun`` accepts, so a shared flat schema can
never forward ``ref`` to ``navigate`` or ``expression`` to ``click``.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool
from .browser_click import BrowserClickTool
from .browser_navigate import BrowserNavigateTool
from .browser_press import BrowserPressTool
from .browser_screenshot import BrowserScreenshotTool
from .browser_scroll import BrowserScrollTool
from .browser_snapshot import BrowserSnapshotTool
from .browser_type import BrowserTypeTool

BROWSER_ACTIONS: tuple[str, ...] = (
    "navigate",
    "snapshot",
    "click",
    "type",
    "press",
    "scroll",
    "screenshot",
)

_HANDLERS: dict[str, BrowserTool] = {
    "navigate": BrowserNavigateTool(),
    "snapshot": BrowserSnapshotTool(),
    "click": BrowserClickTool(),
    "type": BrowserTypeTool(),
    "press": BrowserPressTool(),
    "scroll": BrowserScrollTool(),
    "screenshot": BrowserScreenshotTool(),
}

# The action's own required fields; click is special (ref OR x+y).
_REQUIRED: dict[str, tuple[str, ...]] = {
    "navigate": ("url",),
    "type": ("text",),
    "press": ("keys",),
}


class BrowserInput(BrowserPageInput):
    """Arguments of the merged ``browser`` tool; only the action's fields are used."""

    action: str = Field(
        description=(
            "One of: navigate, snapshot, click, type, press, scroll, screenshot. "
            "Required: navigate→url; click→ref or both x,y; type→text; press→keys; "
            "snapshot/scroll/screenshot→no required fields."
        ),
        json_schema_extra={"enum": list(BROWSER_ACTIONS)},
    )
    url: str | None = Field(
        default=None, description="Absolute URL to open (https://… or http://…); navigate."
    )
    max_elements: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Cap on interactive elements (default 200); snapshot.",
    )
    ref: str | None = Field(
        default=None, description='Element ref from a snapshot (e.g. "e5"); click/type.'
    )
    x: float | None = Field(default=None, description="Viewport x, when clicking by coordinate.")
    y: float | None = Field(default=None, description="Viewport y, when clicking by coordinate.")
    button: str = Field(default="left", description="Mouse button: left / middle / right; click.")
    double: bool = Field(default=False, description="Double click; click.")
    text: str | None = Field(
        default=None,
        description="Text to type into the focused element (or the one named by ref); type.",
    )
    submit: bool = Field(default=False, description="Press Enter after typing; type.")
    keys: str | None = Field(
        default=None,
        description=(
            "One named key: Enter / Tab / Escape / Backspace / Delete / ArrowUp / ArrowDown / "
            "ArrowLeft / ArrowRight / PageUp / PageDown / Home / End / Space; press."
        ),
    )
    delta_y: float = Field(
        default=0, description="Vertical wheel delta (positive scrolls down); scroll."
    )
    delta_x: float = Field(default=0, description="Horizontal wheel delta; scroll.")
    full_page: bool = Field(
        default=False, description="Capture beyond the viewport (whole page); screenshot."
    )


def _accepted(handler: BrowserTool) -> set[str]:
    """The parameter names this handler's ``_arun`` actually accepts."""
    parameters = inspect.signature(handler._arun).parameters
    return {name for name in parameters if name not in {"self", "session_id", "run_manager"}}


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _error(action: str, message: str) -> str:
    required = _REQUIRED.get(action, ())
    suffix = f" Required: {', '.join(required)}." if required else ""
    return f"Error: browser action='{action}': {message}.{suffix}"


class BrowserToolMerged(BrowserTool):
    """The model-facing ``browser`` tool over the seven common verbs."""

    name: str = "browser"
    description: str = (
        "Control the session's live browser page. Choose action, then pass that action's fields: "
        "navigate→url; snapshot→read page text plus [e5]-style refs; click→ref or both x,y; "
        "type→text (optional ref/submit); press→keys; scroll→delta_y; screenshot→full_page. "
        "Snapshot before acting; refs die on navigation."
    )
    args_schema: type[BaseModel] = BrowserInput
    metadata: dict = MAIN_ONLY

    def _unknown_action(self, action: str) -> str:
        if action == "evaluate":
            return (
                "Error: browser action='evaluate' is not part of this tool; use the separate "
                "browser_evaluate tool (available only when the escape hatch is enabled)."
            )
        return (
            f"Error: browser has unknown action '{action}'. "
            f"Available: {', '.join(BROWSER_ACTIONS)}."
        )

    async def _arun(self, action: str, **kwargs: Any) -> str | list[dict[str, Any]]:
        handler = _HANDLERS.get(action)
        if handler is None:
            return self._unknown_action(action)

        missing = [name for name in _REQUIRED.get(action, ()) if _is_missing(kwargs.get(name))]
        if action == "click" and _is_missing(kwargs.get("ref")):
            if kwargs.get("x") is None or kwargs.get("y") is None:
                missing.append("ref or (x and y)")
        if missing:
            return _error(action, f"missing required parameter(s): {', '.join(missing)}")

        accepted = _accepted(handler)
        payload = {
            name: value for name, value in kwargs.items() if name in accepted and value is not None
        }
        payload["session_id"] = kwargs.get("session_id", "")
        try:
            return await handler._arun(**payload)
        except Exception as exc:  # noqa: BLE001 - tool boundary returns readable text
            return f"Error: browser action='{action}' failed: {type(exc).__name__}: {exc}"


__all__ = ["BROWSER_ACTIONS", "BrowserInput", "BrowserToolMerged"]
