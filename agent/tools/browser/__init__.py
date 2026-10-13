"""The agent's browser tools (``browser_*``) — the main agent's own CDP hand.

Usage:

    from agent.tools.browser import build_browser_tools

The tools are thin shells: every verb resolves the process-wide
``BrowserManager`` through ``runtime/hooks.BROWSER_MANAGER`` (``agent/**`` must
not import ``server/**``) and the session comes from ``InjectedState``. All the
bounds (page caps, navigation/op timeouts, element/text/screenshot limits) live
in ``BROWSER_AGENT`` and are enforced by the manager. The per-verb modules
(``tools/browser_*.py``) hold one tool each, so a description or a schema is
edited where it is read.

Two deliberate properties:

* **Main-agent only.** The tools carry ``metadata["scope"] = "main_only"`` —
  the same gate ``memory`` and the taskflow family use — so the subagent tool
  policy drops them unconditionally (ZCode parity: its browser broker refuses
  subagents outright).
* **Off by default.** ``build_browser_tools()`` returns ``[]`` while
  ``BROWSER_AGENT["enabled"]`` is 0: nothing reaches the tool catalogue, the
  prompt, or ``GET /agent/catalog``. ``browser_evaluate``
  additionally requires ``allow_evaluate``.

Reading strategy: ``browser_snapshot`` gives TEXT — the page's readable text
plus numbered interactive elements whose refs ``browser_click`` /
``browser_type`` resolve — and ``browser_screenshot`` is for the visual case
only (the PNG lands in the scratch dir; the ``image_to_text`` skill is how the
model actually looks at it).
"""

from .tools import (
    BrowserClickTool,
    BrowserEvaluateTool,
    BrowserNavigateTool,
    BrowserPressTool,
    BrowserScreenshotTool,
    BrowserScrollTool,
    BrowserSnapshotTool,
    BrowserTypeTool,
    build_browser_tools,
)

__all__ = [
    "BrowserClickTool",
    "BrowserEvaluateTool",
    "BrowserNavigateTool",
    "BrowserPressTool",
    "BrowserScreenshotTool",
    "BrowserScrollTool",
    "BrowserSnapshotTool",
    "BrowserTypeTool",
    "build_browser_tools",
]
