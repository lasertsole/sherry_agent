"""The browser tool family: one module per verb + the assembly factory.

``build_browser_tools`` returns ``[]`` while the feature is switched off (no
schema reaches the catalogue, the prompt or ``GET /agent/catalog``) and adds
``browser_evaluate`` only when ``allow_evaluate`` is set — the escape hatch is
opt-in, like ZCode's.

Every tool class carries ``metadata["scope"] = "main_only"`` (see
``_shared.MAIN_ONLY``), so the subagent tool policy drops the family before the
allow/deny gate even looks at it.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool
from loguru import logger

from config.features import BROWSER_AGENT

from .browser_click import BrowserClickTool, ClickInput
from .browser_evaluate import BrowserEvaluateTool, EvaluateInput
from .browser_navigate import BrowserNavigateTool, NavigateInput
from .browser_press import BrowserPressTool, PressInput
from .browser_screenshot import BrowserScreenshotTool, ScreenshotInput
from .browser_scroll import BrowserScrollTool, ScrollInput
from .browser_snapshot import BrowserSnapshotTool, SnapshotInput
from .browser_type import BrowserTypeTool, TypeInput

__all__ = [
    "BrowserClickTool",
    "BrowserEvaluateTool",
    "BrowserNavigateTool",
    "BrowserPressTool",
    "BrowserScreenshotTool",
    "BrowserScrollTool",
    "BrowserSnapshotTool",
    "BrowserTypeTool",
    "ClickInput",
    "EvaluateInput",
    "NavigateInput",
    "PressInput",
    "ScreenshotInput",
    "ScrollInput",
    "SnapshotInput",
    "TypeInput",
    "build_browser_tools",
]


def build_browser_tools() -> list[BaseTool]:
    """The browser tool set — empty while the feature is switched off."""
    if not BROWSER_AGENT["enabled"]:
        return []
    tools: list[BaseTool] = [
        BrowserNavigateTool(),
        BrowserSnapshotTool(),
        BrowserClickTool(),
        BrowserTypeTool(),
        BrowserPressTool(),
        BrowserScrollTool(),
        BrowserScreenshotTool(),
    ]
    if BROWSER_AGENT["allow_evaluate"]:
        tools.append(BrowserEvaluateTool())
    logger.debug(
        "Browser tools registered: {} (evaluate={})",
        len(tools),
        bool(BROWSER_AGENT["allow_evaluate"]),
    )
    return tools
