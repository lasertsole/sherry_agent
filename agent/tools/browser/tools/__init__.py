"""The browser tool family: one merged surface + the opt-in evaluate tool.

``build_browser_tools`` returns ``[]`` while the feature is switched off (no
schema reaches the catalogue, the prompt or ``GET /agent/catalog``). When it is
on, the seven everyday verbs arrive as ONE ``browser`` tool; ``browser_evaluate``
is added only when ``allow_evaluate`` is set — the escape hatch stays opt-in and
is absent, not merely disabled, while the switch is off.

The original per-verb classes remain in this package as the merged tool's
handlers (their text formatting and ``guard`` error contract are unchanged), and
``build_legacy_browser_tools()`` exposes them for tests/A-B comparisons.

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
from .browser_merged import BROWSER_ACTIONS, BrowserInput, BrowserToolMerged
from .browser_navigate import BrowserNavigateTool, NavigateInput
from .browser_press import BrowserPressTool, PressInput
from .browser_screenshot import BrowserScreenshotTool, ScreenshotInput
from .browser_scroll import BrowserScrollTool, ScrollInput
from .browser_snapshot import BrowserSnapshotTool, SnapshotInput
from .browser_type import BrowserTypeTool, TypeInput

__all__ = [
    "BROWSER_ACTIONS",
    "BrowserClickTool",
    "BrowserEvaluateTool",
    "BrowserInput",
    "BrowserNavigateTool",
    "BrowserPressTool",
    "BrowserScreenshotTool",
    "BrowserScrollTool",
    "BrowserSnapshotTool",
    "BrowserToolMerged",
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
    "build_legacy_browser_tools",
]


def _legacy_verb_tools() -> list[BaseTool]:
    """The seven pre-merge verb tools, in their original order."""
    return [
        BrowserNavigateTool(),
        BrowserSnapshotTool(),
        BrowserClickTool(),
        BrowserTypeTool(),
        BrowserPressTool(),
        BrowserScrollTool(),
        BrowserScreenshotTool(),
    ]


def build_legacy_browser_tools() -> list[BaseTool]:
    """The original per-verb browser tools for tests/evals (not registered)."""
    if not BROWSER_AGENT["enabled"]:
        return []
    tools = _legacy_verb_tools()
    if BROWSER_AGENT["allow_evaluate"]:
        tools.append(BrowserEvaluateTool())
    return tools


def build_browser_tools() -> list[BaseTool]:
    """The model-facing browser set — empty while the feature is switched off."""
    if not BROWSER_AGENT["enabled"]:
        return []
    tools: list[BaseTool] = [BrowserToolMerged()]
    if BROWSER_AGENT["allow_evaluate"]:
        tools.append(BrowserEvaluateTool())
    logger.debug(
        "Browser tools registered: {} (evaluate={})",
        [tool.name for tool in tools],
        bool(BROWSER_AGENT["allow_evaluate"]),
    )
    return tools
