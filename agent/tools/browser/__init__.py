"""The agent's browser tools (``browser`` + optional ``browser_evaluate``).

Usage:

    from agent.tools.browser import build_browser_tools

The model-facing ``browser`` tool is one action dispatcher over seven thin
handlers; each handler resolves the process-wide ``BrowserManager`` through
``runtime/hooks.BROWSER_MANAGER`` (``agent/**`` must not import ``server/**``)
and the session comes from ``InjectedState``. All the bounds (page caps,
navigation/op timeouts, element/text/screenshot limits) live in
``BROWSER_AGENT`` and are enforced by the manager. The per-verb modules
(``tools/browser_*.py``) remain the handlers, so a description or a manager call
is edited where it is read.

Two deliberate properties:

* **Main-agent only.** The tools carry ``metadata["scope"] = "main_only"`` —
  the same gate ``memory`` and the taskflow family use — so the subagent tool
  policy drops them unconditionally (ZCode parity: its browser broker refuses
  subagents outright).
* **Off by default.** ``build_browser_tools()`` returns ``[]`` while
  ``BROWSER_AGENT["enabled"]`` is 0: nothing reaches the tool catalogue, the
  prompt, or ``GET /agent/catalog``. ``browser_evaluate`` is a SEPARATE tool
  that additionally requires ``allow_evaluate``; it is absent, not disabled,
  while the escape hatch is off.

Reading strategy: ``browser(action="snapshot")`` gives TEXT — the page's
readable text plus numbered interactive elements whose refs
``browser(action="click"/"type", ref=...)`` resolve — and
``browser(action="screenshot")`` is for the visual case only (the PNG lands in
the scratch dir; the ``image_to_text`` skill is how a model without native
vision looks at it).
"""

from .tools import (
    BROWSER_ACTIONS,
    BrowserInput,
    BrowserToolMerged,
    build_browser_tools,
    build_legacy_browser_tools,
)

__all__ = [
    "BROWSER_ACTIONS",
    "BrowserInput",
    "BrowserToolMerged",
    "build_browser_tools",
    "build_legacy_browser_tools",
]
