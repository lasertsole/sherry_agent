"""The agent's browser tools (``browser_*``) — the main agent's own CDP hand.

The tools are thin shells: every verb resolves the process-wide
``BrowserManager`` through ``runtime/hooks.BROWSER_MANAGER`` (``agent/**`` must
not import ``server/**``) and the session comes from ``InjectedState``. All the
bounds (page caps, navigation/op timeouts, element/text/screenshot limits) live
in ``BROWSER_AGENT`` and are enforced by the manager.

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

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import BaseTool
from loguru import logger
from langgraph.prebuilt.tool_node import InjectedState
from pydantic import BaseModel, Field

from config.features import BROWSER_AGENT

__all__ = ["build_browser_tools"]

SessionId = Annotated[str, InjectedState("session_id")]

#: The subagent tool policy drops these unconditionally (see module docstring).
_MAIN_ONLY = {"scope": "main_only"}


def _resolve_manager() -> Any:
    """The process-wide manager, via the hook registry.

    :raises RuntimeError: no server assembly registered the bridge.
    """
    from runtime import hooks

    resolver = hooks.resolve(hooks.BROWSER_MANAGER)
    if resolver is None:
        raise RuntimeError(
            "the browser bridge is not assembled in this process — start the "
            "server to use the browser tools"
        )
    return resolver()


async def _guard(operation) -> str:
    """Run one manager call, turning expected failures into tool-result text.

    ``RuntimeError`` also covers the server's ``CdpError`` (a ``RuntimeError``
    subclass) — the class itself cannot be imported here: ``agent/**`` must not
    import ``server/**``.
    """
    try:
        return await operation()
    except KeyError as error:
        return f"Error: {error.args[0] if error.args else error}"
    except (ValueError, PermissionError, RuntimeError) as error:
        return f"Error: {error}"


def _page_line(info: dict[str, Any]) -> str:
    title = info.get("title") or "(no title)"
    return f"page {info.get('page')} · «{title}» · {info.get('url', '')}"


class _BrowserTool(BaseTool):
    """Shared base: every verb is async-only.

    The manager is loop-bound (locks + one WebSocket on the server loop), so a
    sync call must not quietly run on another loop — it fails with a message
    that says where to call from instead. The agent graph invokes tools through
    ``ainvoke``, which uses the overridden ``_arun`` and never touches this.
    """

    def _run(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("browser tools are asynchronous — invoke them through the agent graph")


def _format_snapshot(outcome: dict[str, Any]) -> str:
    """Snapshot → readable text: the page text, then the ref-addressable elements."""
    lines = [_page_line(outcome)]
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


# ------------------------------------------------------------------- schemas


class NavigateInput(BaseModel):
    """Arguments of ``browser_navigate``."""

    url: str = Field(description="Absolute URL to open (https://… or http://…).")
    page: str | None = Field(
        default=None, description="Page id from an earlier call; omit for the session's page."
    )
    session_id: SessionId = ""


class SnapshotInput(BaseModel):
    """Arguments of ``browser_snapshot``."""

    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    max_elements: int | None = Field(
        default=None, ge=1, le=1000, description="Cap on interactive elements (default 200)."
    )
    session_id: SessionId = ""


class ClickInput(BaseModel):
    """Arguments of ``browser_click``."""

    ref: str | None = Field(default=None, description='Element ref from a snapshot (e.g. "e5").')
    x: float | None = Field(
        default=None, description="Viewport x, when clicking by coordinate instead of ref."
    )
    y: float | None = Field(
        default=None, description="Viewport y, when clicking by coordinate instead of ref."
    )
    button: str = Field(default="left", description="Mouse button: left / middle / right.")
    double: bool = Field(default=False, description="Double click.")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class TypeInput(BaseModel):
    """Arguments of ``browser_type``."""

    text: str = Field(
        description="Text to type into the focused element (or the one named by ref)."
    )
    ref: str | None = Field(
        default=None,
        description="Input ref from a snapshot; omit to type into whatever is focused.",
    )
    submit: bool = Field(default=False, description="Press Enter after typing.")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class PressInput(BaseModel):
    """Arguments of ``browser_press``."""

    keys: str = Field(
        description="One named key: Enter / Tab / Escape / Backspace / Delete / "
        "ArrowUp / ArrowDown / ArrowLeft / ArrowRight / PageUp / PageDown / Home / End / Space."
    )
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class ScrollInput(BaseModel):
    """Arguments of ``browser_scroll``."""

    delta_y: float = Field(default=0, description="Vertical wheel delta (positive scrolls down).")
    delta_x: float = Field(default=0, description="Horizontal wheel delta.")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class ScreenshotInput(BaseModel):
    """Arguments of ``browser_screenshot``."""

    full_page: bool = Field(default=False, description="Capture beyond the viewport (whole page).")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


class EvaluateInput(BaseModel):
    """Arguments of ``browser_evaluate``."""

    expression: str = Field(description="JavaScript expression (JSON-serializable result).")
    page: str | None = Field(default=None, description="Page id; omit for the session's page.")
    session_id: SessionId = ""


# --------------------------------------------------------------------- tools


class BrowserNavigateTool(_BrowserTool):
    name: str = "browser_navigate"
    description: str = (
        "Open a URL in the session's browser page (the page is created on first "
        "use) and wait for it to load. Returns the final URL, title and whether "
        "the load finished. Read the page with browser_snapshot afterwards — "
        "snapshot refs die on navigation. The browser is a real page a person "
        "can watch and take over in the 工具箱·浏览器 panel."
    )
    args_schema: type[BaseModel] = NavigateInput
    metadata: dict = _MAIN_ONLY

    async def _arun(self, url: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.navigate(session_id, url, page)
            state = "loaded" if outcome.get("loaded") else "still loading (timed out waiting)"
            return f"{_page_line(outcome)} — {state}"

        return await _guard(operation)


class BrowserSnapshotTool(_BrowserTool):
    name: str = "browser_snapshot"
    description: str = (
        "Read the page: its visible text plus the interactive elements (buttons, "
        "links, inputs) numbered as refs like [e5]. Pass a ref to browser_click / "
        "browser_type. Prefer this over browser_screenshot — text is cheaper and "
        "sharper; screenshots are for layout/visual questions only."
    )
    args_schema: type[BaseModel] = SnapshotInput
    metadata: dict = _MAIN_ONLY

    async def _arun(
        self, page: str | None = None, max_elements: int | None = None, session_id: str = ""
    ) -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.snapshot(session_id, page, max_elements=max_elements)
            return _format_snapshot(outcome)

        return await _guard(operation)


class BrowserClickTool(_BrowserTool):
    name: str = "browser_click"
    description: str = (
        'Click an element by its snapshot ref (e.g. "e5"), or by viewport x/y '
        "coordinates. Returns what was clicked. Re-snapshot before the next "
        "action when the page changed."
    )
    args_schema: type[BaseModel] = ClickInput
    metadata: dict = _MAIN_ONLY

    async def _arun(
        self,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.click(
                session_id, ref=ref, x=x, y=y, button=button, double=double, page_id=page
            )
            label = outcome.get("text") or outcome.get("tag") or ""
            suffix = f" («{label}»)" if label else ""
            return f"clicked [{outcome.get('clicked')}]{suffix} on page {outcome.get('page')}"

        return await _guard(operation)


class BrowserTypeTool(_BrowserTool):
    name: str = "browser_type"
    description: str = (
        "Type text into an input: pass a snapshot ref (recommended) or omit ref "
        "to type into the currently focused element. Set submit=true to press "
        "Enter afterwards (e.g. to run a search)."
    )
    args_schema: type[BaseModel] = TypeInput
    metadata: dict = _MAIN_ONLY

    async def _arun(
        self,
        text: str,
        ref: str | None = None,
        submit: bool = False,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.type_text(
                session_id, text, ref=ref, submit=submit, page_id=page
            )
            tail = " and pressed Enter" if outcome.get("submitted") else ""
            return f"typed {outcome.get('typed')} character(s){tail} on page {outcome.get('page')}"

        return await _guard(operation)


class BrowserPressTool(_BrowserTool):
    name: str = "browser_press"
    description: str = (
        "Press one named key on the page (Enter / Tab / Escape / arrows / "
        "PageUp / PageDown / Home / End / Backspace / Delete / Space)."
    )
    args_schema: type[BaseModel] = PressInput
    metadata: dict = _MAIN_ONLY

    async def _arun(self, keys: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.press(session_id, keys, page)
            return f"pressed {outcome.get('pressed')} on page {outcome.get('page')}"

        return await _guard(operation)


class BrowserScrollTool(_BrowserTool):
    name: str = "browser_scroll"
    description: str = (
        "Scroll the page by a wheel delta (positive delta_y scrolls down). Use "
        "small steps and re-snapshot to see the new content."
    )
    args_schema: type[BaseModel] = ScrollInput
    metadata: dict = _MAIN_ONLY

    async def _arun(
        self,
        delta_y: float = 0,
        delta_x: float = 0,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.scroll(
                session_id, delta_y=delta_y, delta_x=delta_x, page_id=page
            )
            return f"scrolled {outcome.get('scrolled')} on page {outcome.get('page')}"

        return await _guard(operation)


class BrowserScreenshotTool(_BrowserTool):
    name: str = "browser_screenshot"
    description: str = (
        "Capture the page (or, with full_page=true, the whole scrollable page) "
        "as a PNG saved on disk; returns the path. To actually LOOK at it, run "
        "the image_to_text skill on that path (terminal tool). Prefer "
        "browser_snapshot for reading content."
    )
    args_schema: type[BaseModel] = ScreenshotInput
    metadata: dict = _MAIN_ONLY

    async def _arun(
        self, full_page: bool = False, page: str | None = None, session_id: str = ""
    ) -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.screenshot(session_id, full_page=full_page, page_id=page)
            size = (
                f"{outcome.get('width')}×{outcome.get('height')}"
                if outcome.get("width")
                else f"{outcome.get('bytes')} bytes"
            )
            return (
                f"screenshot saved: {outcome.get('path')} ({size}). "
                "Use the image_to_text skill to view it."
            )

        return await _guard(operation)


class BrowserEvaluateTool(_BrowserTool):
    name: str = "browser_evaluate"
    description: str = (
        "Run one JavaScript expression in the page and get its JSON value back. "
        "The escape hatch for what the typed tools cannot express (reading "
        "computed data, scrolling inside a container, extracting a table). The "
        "page is live: an expression can change it."
    )
    args_schema: type[BaseModel] = EvaluateInput
    metadata: dict = _MAIN_ONLY

    async def _arun(self, expression: str, page: str | None = None, session_id: str = "") -> str:
        async def operation() -> str:
            manager = _resolve_manager()
            outcome = await manager.evaluate(session_id, expression, page)
            tail = " (truncated)" if outcome.get("truncated") else ""
            return f"page {outcome.get('page')} result{tail}: {outcome.get('result')}"

        return await _guard(operation)


def build_browser_tools() -> list[BaseTool]:
    """The browser tool set — empty while the feature is switched off.

    ``browser_evaluate`` joins only when ``allow_evaluate`` is set (the
    escape hatch is opt-in, like ZCode's).
    """
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
