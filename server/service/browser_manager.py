"""The browser manager: one Chromium, pages per session, the agent's verbs.

Process singleton (the server runs single-process): ``get_browser_manager()``
returns the one instance, which launches Chromium lazily on the first use and
keeps the pages in a registry keyed by ``page_id``. A page belongs to a SHERRY
session; the newest page of a session is its default handle, so the agent's
tools can omit the ``page`` argument.

Design points that the plan's invariants pin:

* C2 — one Chromium per process; ``status()`` reports the page count, never the
  debug port.
* C5 — the panel is a SECOND independent CDP client; nothing here locks reads,
  only navigation (a per-page ``asyncio.Lock``), so a person can keep using the
  page while the agent works.
* C6 — every operation is bounded (timeouts, element/text caps, screenshot size
  cap) and reports ``truncated`` instead of hanging.
* Refs — a snapshot assigns ``eN`` handles and stores ``ref -> xpath`` on the
  SERVER (cleared on navigation). ``browser_click(ref=...)`` resolves through
  that map, so a click is a plain typed call, not an opaque script.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Callable

from loguru import logger

from config.features import BROWSER_AGENT, BrowserAgentConfig
from config.path import TEMP_DIR
from server.service.browser_cdp import (
    CdpConnection,
    CdpError,
    launch_browser,
    resolve_executable,
)

__all__ = ["BrowserManager", "BrowserPage", "get_browser_manager", "reset_browser_manager"]


class BrowserTransport(Protocol):
    """The slice of :class:`CdpConnection` the manager depends on (fake-able)."""

    @property
    def connected(self) -> bool: ...

    async def start(self, *, timeout: float = ...) -> None: ...

    async def close(self) -> None: ...

    async def call(
        self, method: str, session: str | None = None, *, timeout: float | None = ..., **params: Any
    ) -> dict[str, Any]: ...

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None: ...

    def remove_listener(self, listener: Callable[[dict[str, Any]], None]) -> None: ...


@dataclass
class BrowserPage:
    """One live tab: its CDP session, its owner, its refs and its subscribers."""

    page_id: str
    target_id: str
    cdp_session: str
    session_id: str
    url: str = "about:blank"
    title: str = ""
    refs: dict[str, str] = field(default_factory=dict)
    nav_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = 0.0
    last_used: float = 0.0
    framers: list[Callable[[BrowserPage, dict[str, Any]], None]] = field(default_factory=list)
    #: Listeners for this page's raw CDP events (method + params), e.g. the
    #: panel following a user-initiated navigation with Page.frameNavigated.
    event_sinks: list[Callable[[BrowserPage, str, dict[str, Any]], None]] = field(
        default_factory=list
    )
    alive: bool = True
    screencasting: bool = False
    #: "page" for an ordinary tab, "devtools" for a devtools frontend target.
    kind: str = "page"
    #: For kind="devtools": the target id this frontend inspects.
    devtools_for: str = ""

    def info(self) -> dict[str, Any]:
        """The JSON-safe shape routes and tools report for this page."""
        return {
            "page": self.page_id,
            "url": self.url,
            "title": self.title,
            "session_id": self.session_id,
            "kind": self.kind,
        }


#: The snapshot probe: interactive elements + a readable text excerpt.
#: ``__MAX_ELEMENTS__``/``__MAX_TEXT__`` are substituted before evaluation.
_SNAPSHOT_JS = r"""
(() => {
  const MAX = __MAX_ELEMENTS__;
  const MAXTEXT = __MAX_TEXT__;
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const xpathOf = (el) => {
    if (el.id) {
      const escaped = String(el.id).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      return '//*[@id="' + escaped + '"]';
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      const tag = node.tagName.toLowerCase();
      if (tag === 'html' || tag === 'body') break;
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(tag + '[' + index + ']');
      node = node.parentElement;
    }
    return '//' + parts.join('/');
  };
  const nameOf = (el) => String(
    el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
    el.getAttribute('alt') || el.innerText || el.value ||
    el.getAttribute('title') || ''
  ).trim().replace(/\s+/g, ' ').slice(0, 140);
  const SELECTOR = 'a,button,input,select,textarea,summary,' +
    '[role="button"],[role="link"],[role="tab"],[role="menuitem"],' +
    '[contenteditable="true"],[onclick]';
  const elements = [];
  let truncated = false;
  let count = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!visible(el)) continue;
    if (count >= MAX) { truncated = true; break; }
    count += 1;
    elements.push({
      ref: 'e' + count,
      xpath: xpathOf(el),
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      role: el.getAttribute('role') || null,
      name: nameOf(el),
      value: (typeof el.value === 'string') ? el.value.slice(0, 140) : null,
      href: el.href ? String(el.href).slice(0, 300) : null,
      checked: (typeof el.checked === 'boolean') ? el.checked : null,
    });
  }
  const text = String(document.body ? document.body.innerText : '')
    .replace(/\n{3,}/g, '\n\n').slice(0, MAXTEXT);
  return JSON.stringify({
    url: location.href,
    title: document.title,
    text,
    elements,
    truncated,
  });
})()
"""

#: Key name -> (code, windowsVirtualKeyCode). Enough for the form/scroll verbs.
_KEY_TABLE: dict[str, tuple[str, int]] = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "Space": ("Space", 32),
}

#: Payload keys each raw input kind may carry (anything else is dropped).
_INPUT_KEYS: dict[str, tuple[str, ...]] = {
    "mouse": ("type", "x", "y", "button", "clickCount", "modifiers", "buttons", "pointerType"),
    "wheel": ("x", "y", "deltaX", "deltaY", "modifiers"),
    "key": (
        "type",
        "key",
        "code",
        "text",
        "windowsVirtualKeyCode",
        "nativeVirtualKeyCode",
        "modifiers",
        "autoRepeat",
    ),
}

#: Longest JSON-serialized evaluate result served back (C6).
_EVALUATE_MAX_CHARS = 8000


def _png_size(png: bytes) -> tuple[int, int] | None:
    """Width/height from a PNG IHDR, or ``None`` when the bytes are not a PNG."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", png[16:24])
    return width, height


class BrowserManager:
    """The process-wide browser: lazy launch, per-session pages, bounded verbs."""

    def __init__(
        self,
        config: BrowserAgentConfig | None = None,
        *,
        launcher: Callable[..., Any] = launch_browser,
        transport_factory: Callable[..., BrowserTransport] = CdpConnection,
    ) -> None:
        self.config = config if config is not None else BROWSER_AGENT
        self._launcher = launcher
        self._transport_factory = transport_factory
        self._transport: BrowserTransport | None = None
        self._pid: int | None = None
        #: Debug port, used ONLY to build devtools frontend URLs in-process (C3).
        self._port: int | None = None
        self._start_lock = asyncio.Lock()
        self._pages: dict[str, BrowserPage] = {}
        self._counter = 0

    # ---------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        """True when the feature is switched on (tools/routes exist)."""
        return bool(self.config["enabled"])

    @property
    def running(self) -> bool:
        """True while a transport is connected to a live browser."""
        return self._transport is not None and self._transport.connected

    def status(self) -> dict[str, Any]:
        """The panel's / the tools' view: never the debug port (C3)."""
        return {
            "enabled": self.enabled,
            "running": self.running,
            "pid": self._pid,
            "pages": len(self._pages),
            "sessions": len({page.session_id for page in self._pages.values()}),
        }

    async def ensure_started(self) -> None:
        """Launch Chromium and connect on first use (idempotent, serialized)."""
        if self.running:
            return
        async with self._start_lock:
            if self.running:
                return
            executable = resolve_executable(
                self.config["executable"], self.config["executable_candidates"]
            )
            launched = await self._launcher(
                executable=executable,
                headless=bool(self.config["headless"]),
                user_data_dir=self.config["user_data_dir"],
                timeout_s=float(self.config["launch_timeout_s"]),
            )
            transport = self._transport_factory(launched.ws_url)
            await transport.start(timeout=float(self.config["launch_timeout_s"]))
            transport.add_listener(self._on_event)
            self._transport = transport
            self._pid = launched.pid
            self._port = launched.port

    async def shutdown(self) -> None:
        """Close every page, the browser and the transport (idempotent)."""
        transport = self._transport
        self._transport = None
        self._pid = None
        self._port = None
        self._pages.clear()
        if transport is None:
            return
        try:
            await transport.call("Browser.close", timeout=3.0)
        except Exception:  # noqa: BLE001 - a dead browser is already the goal
            logger.debug("Browser.close failed during shutdown (already gone?)")
        await transport.close()

    # -------------------------------------------------------------------- pages

    def list_pages(self, session_id: str) -> list[dict[str, Any]]:
        """Every live page of one session, newest activity first."""
        pages = [page for page in self._pages.values() if page.session_id == session_id]
        pages.sort(key=lambda page: page.last_used, reverse=True)
        return [page.info() for page in pages]

    def page_for(self, session_id: str, page_id: str | None = None) -> BrowserPage:
        """Resolve a session's page: the named one, else its most recent.

        The DEFAULT resolution prefers ordinary pages: a devtools frontend is an
        auxiliary target (kind="devtools") and must never become the implicit
        subject of a navigate / snapshot / click. It is only reached by naming
        it or when nothing else exists.

        :raises KeyError: the session has no page (and none was named).
        """
        if page_id:
            page = self._pages.get(page_id)
            if page is None or page.session_id != session_id:
                raise KeyError(f"unknown page {page_id!r} for this session")
            return page
        candidates = [page for page in self._pages.values() if page.session_id == session_id]
        if not candidates:
            raise KeyError(
                f"session {session_id!r} has no browser page — call browser_navigate first"
            )
        ordinary = [page for page in candidates if page.kind == "page"]
        return max(ordinary or candidates, key=lambda page: page.last_used)

    async def open_page(self, session_id: str, url: str = "about:blank") -> BrowserPage:
        """Create a page for a session (evicting the global LRU when full)."""
        await self.ensure_started()
        transport = self._require_transport()
        await self._evict_lru_if_needed()
        created = await transport.call(
            "Target.createTarget", url=url, timeout=float(self.config["op_timeout_s"])
        )
        attached = await transport.call(
            "Target.attachToTarget",
            targetId=created["targetId"],
            flatten=True,
            timeout=float(self.config["op_timeout_s"]),
        )
        self._counter += 1
        page = BrowserPage(
            page_id=f"p{self._counter}",
            target_id=created["targetId"],
            cdp_session=attached["sessionId"],
            session_id=session_id,
            url=url,
            created_at=time.monotonic(),
            last_used=time.monotonic(),
        )
        await transport.call("Page.enable", session=page.cdp_session)
        self._pages[page.page_id] = page
        return page

    async def close_page(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """Close one page (the session's default when ``page_id`` is omitted)."""
        page = self.page_for(session_id, page_id)
        target_id = page.target_id
        self._pages.pop(page.page_id, None)
        transport = self._transport
        if transport is not None and transport.connected:
            try:
                await transport.call("Target.closeTarget", targetId=target_id, timeout=3.0)
            except CdpError:
                pass
        return {"page": page.page_id, "closed": True}

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Close every page of one session (session deletion / teardown).

        A no-op with the feature off or the browser never started: the registry
        is keyed by session and nothing else would ever close these pages (the
        LRU only caps the global count).
        """
        pages = [page for page in self._pages.values() if page.session_id == session_id]
        for page in pages:
            self._pages.pop(page.page_id, None)
        transport = self._transport
        if transport is not None and transport.connected:
            for page in pages:
                try:
                    await transport.call("Target.closeTarget", targetId=page.target_id, timeout=3.0)
                except CdpError:
                    pass
        return {"session_id": session_id, "closed": len(pages)}

    async def _evict_lru_if_needed(self) -> None:
        """Keep the page count under ``max_pages`` by closing the stalest page."""
        while len(self._pages) >= int(self.config["max_pages"]):
            stalest = min(self._pages.values(), key=lambda page: page.last_used)
            self._pages.pop(stalest.page_id, None)
            transport = self._transport
            if transport is not None and transport.connected:
                try:
                    await transport.call(
                        "Target.closeTarget", targetId=stalest.target_id, timeout=3.0
                    )
                except CdpError:
                    pass

    # ------------------------------------------------------------------- events

    def _on_event(self, message: dict[str, Any]) -> None:
        """Route one transport event to its page (screencast frames fan out)."""
        session = message.get("sessionId")
        if not session:
            return
        page = next(
            (candidate for candidate in self._pages.values() if candidate.cdp_session == session),
            None,
        )
        if page is None:
            return
        method = message.get("method", "")
        if method == "Page.screencastFrame":
            params = message.get("params", {})
            for framer in list(page.framers):
                try:
                    framer(page, params)
                except Exception:  # noqa: BLE001 - a bad subscriber must not kill the feed
                    logger.opt(exception=True).warning("screencast subscriber failed")
        elif method in ("Inspector.detached", "Target.detachedFromTarget"):
            page.alive = False
        for sink in list(page.event_sinks):
            try:
                sink(page, method, message.get("params") or {})
            except Exception:  # noqa: BLE001 - a bad subscriber must not kill the feed
                logger.opt(exception=True).warning("page event subscriber failed")

    def subscribe_frames(
        self, page: BrowserPage, framer: Callable[[BrowserPage, dict[str, Any]], None]
    ) -> None:
        """Register a screencast-frame subscriber for one page."""
        if framer not in page.framers:
            page.framers.append(framer)

    def unsubscribe_frames(
        self, page: BrowserPage, framer: Callable[[BrowserPage, dict[str, Any]], None]
    ) -> None:
        """Drop a screencast-frame subscriber (no-op when absent)."""
        if framer in page.framers:
            page.framers.remove(framer)

    def subscribe_events(
        self, page: BrowserPage, sink: Callable[[BrowserPage, str, dict[str, Any]], None]
    ) -> None:
        """Register a raw-page-event listener (``method`` + ``params``)."""
        if sink not in page.event_sinks:
            page.event_sinks.append(sink)

    def unsubscribe_events(
        self, page: BrowserPage, sink: Callable[[BrowserPage, str, dict[str, Any]], None]
    ) -> None:
        """Drop a raw-page-event listener (no-op when absent)."""
        if sink in page.event_sinks:
            page.event_sinks.remove(sink)

    async def start_screencast(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """Start JPEG frame streaming to subscribers (the panel's picture).

        A freshly loaded page paints nothing new, so no frame arrives until the
        page changes — measured on this host: a static page emitted zero frames
        in 15 s. One tiny DOM touch (:meth:`_nudge_paint`) forces the first
        frame, then real navigation/typing keeps the stream moving.
        """
        page = self.page_for(session_id, page_id)
        transport = self._require_transport()
        await transport.call(
            "Page.startScreencast",
            session=page.cdp_session,
            format="jpeg",
            quality=int(self.config["screencast_quality"]),
            maxWidth=int(self.config["screencast_max_width"]),
            maxHeight=int(self.config["screencast_max_height"]),
            everyNthFrame=1,
            timeout=float(self.config["op_timeout_s"]),
        )
        page.screencasting = True
        await self._nudge_paint(page)
        return {**page.info(), "screencasting": True}

    async def stop_screencast(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """Stop frame streaming for a page (safe when it was never started)."""
        page = self.page_for(session_id, page_id)
        if page.screencasting and self._transport is not None and self._transport.connected:
            try:
                await self._transport.call(
                    "Page.stopScreencast", session=page.cdp_session, timeout=3.0
                )
            except CdpError:
                pass
        page.screencasting = False
        return {**page.info(), "screencasting": False}

    async def _nudge_paint(self, page: BrowserPage) -> None:
        """Touch the DOM once so the compositor emits a fresh frame (best effort)."""
        try:
            await self._eval_raw(
                page,
                "void (document.body && (document.body.dataset.sherryPaint = String(Date.now())))",
                timeout=2.0,
            )
        except (CdpError, ValueError, TypeError):
            pass

    async def set_viewport(
        self,
        session_id: str,
        *,
        width: int,
        height: int,
        device_scale_factor: float = 1,
        mobile: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply the free-size device frame (the panel's width/height/zoom)."""
        page = self.page_for(session_id, page_id)
        await self._require_transport().call(
            "Emulation.setDeviceMetricsOverride",
            session=page.cdp_session,
            width=int(width),
            height=int(height),
            deviceScaleFactor=float(device_scale_factor),
            mobile=bool(mobile),
            timeout=float(self.config["op_timeout_s"]),
        )
        await self._nudge_paint(page)
        return {**page.info(), "viewport": [int(width), int(height)]}

    # --------------------------------------------------------------------- verbs

    async def navigate(
        self, session_id: str, url: str, page_id: str | None = None
    ) -> dict[str, Any]:
        """Navigate a page and wait for its load (bounded by ``nav_timeout_s``).

        A session without a page gets one on the first navigate: the tool's and
        the panel's address bars both treat "open the browser here" as one step.
        """
        target = str(url or "").strip()
        if not target:
            raise ValueError("url is required")
        if page_id is not None:
            page = self.page_for(session_id, page_id)
        else:
            try:
                page = self.page_for(session_id)
            except KeyError:
                page = await self.open_page(session_id, "about:blank")
        async with page.nav_lock:
            transport = self._require_transport()
            await transport.call(
                "Page.navigate",
                session=page.cdp_session,
                url=target,
                timeout=float(self.config["op_timeout_s"]),
            )
            page.refs.clear()
            finished = await self._wait_ready(page, float(self.config["nav_timeout_s"]))
        location = await self._page_location(page)
        page.url = location.get("url") or target
        page.title = location.get("title") or ""
        page.last_used = time.monotonic()
        return {**page.info(), "loaded": finished}

    async def snapshot(
        self,
        session_id: str,
        page_id: str | None = None,
        *,
        max_elements: int | None = None,
    ) -> dict[str, Any]:
        """Read the page: interactive elements (with refs) plus a text excerpt."""
        page = self.page_for(session_id, page_id)
        limit = int(max_elements or self.config["snapshot_max_elements"])
        script = _SNAPSHOT_JS.replace("__MAX_ELEMENTS__", str(limit)).replace(
            "__MAX_TEXT__", str(int(self.config["snapshot_max_text_chars"]))
        )
        raw = await self._eval_raw(page, script, timeout=float(self.config["op_timeout_s"]))
        data = json.loads(raw) if isinstance(raw, str) else {}
        elements = data.get("elements") or []
        page.refs = {str(item["ref"]): str(item["xpath"]) for item in elements}
        for item in elements:
            item.pop("xpath", None)
        page.url = str(data.get("url") or page.url)
        page.title = str(data.get("title") or page.title)
        page.last_used = time.monotonic()
        return {
            **page.info(),
            "text": data.get("text") or "",
            "elements": elements,
            "truncated": bool(data.get("truncated")),
        }

    async def click(
        self,
        session_id: str,
        *,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        double: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Click by snapshot ref (``eN``) or by viewport coordinates."""
        page = self.page_for(session_id, page_id)
        if ref:
            xpath = page.refs.get(str(ref))
            if not xpath:
                raise ValueError(
                    f"unknown ref {ref!r} — take a fresh snapshot (refs die on navigation)"
                )
            script = (
                "(() => { const el = document.evaluate(__XPATH__, document, null, "
                "XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;"
                " if (!el) return JSON.stringify({ok:false});"
                " el.scrollIntoView({block:'center', inline:'center'}); el.click();"
                " return JSON.stringify({ok:true, tag: el.tagName.toLowerCase(),"
                " text: String(el.innerText || el.value || '').trim().slice(0,120)}); })()"
            ).replace("__XPATH__", json.dumps(xpath))
            raw = await self._eval_raw(page, script, timeout=float(self.config["op_timeout_s"]))
            outcome = json.loads(raw) if isinstance(raw, str) else {"ok": False}
            if not outcome.get("ok"):
                raise ValueError(f"ref {ref!r} no longer resolves — take a fresh snapshot")
            page.last_used = time.monotonic()
            return {"page": page.page_id, "clicked": str(ref), **outcome}
        if x is None or y is None:
            raise ValueError("browser_click needs a ref, or an x/y pair")
        transport = self._require_transport()
        clicks = 2 if double else 1
        for kind in ("mousePressed", "mouseReleased"):
            await transport.call(
                "Input.dispatchMouseEvent",
                session=page.cdp_session,
                type=kind,
                x=float(x),
                y=float(y),
                button=button,
                clickCount=clicks,
                timeout=float(self.config["op_timeout_s"]),
            )
        page.last_used = time.monotonic()
        return {"page": page.page_id, "clicked": f"{x},{y}"}

    async def type_text(
        self,
        session_id: str,
        text: str,
        *,
        ref: str | None = None,
        submit: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Type into the page — the focused element, or the one a ref names."""
        page = self.page_for(session_id, page_id)
        transport = self._require_transport()
        if ref:
            xpath = page.refs.get(str(ref))
            if not xpath:
                raise ValueError(
                    f"unknown ref {ref!r} — take a fresh snapshot (refs die on navigation)"
                )
            focus = (
                "(() => { const el = document.evaluate(__XPATH__, document, null, "
                "XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;"
                " if (!el) return 'missing';"
                " el.scrollIntoView({block:'center', inline:'center'}); el.focus();"
                # Select the existing text so Input.insertText REPLACES it — a
                # direct ``el.value = ''`` fights React's value tracker; a
                # selection change is plain user behaviour.
                " if (typeof el.setSelectionRange === 'function') {"
                "   el.setSelectionRange(0, String(el.value || '').length);"
                " } else if (el.isContentEditable && document.execCommand) {"
                "   document.execCommand('selectAll', false, null);"
                " }"
                " return 'ok'; })()"
            ).replace("__XPATH__", json.dumps(xpath))
            focused = await self._eval_raw(page, focus, timeout=float(self.config["op_timeout_s"]))
            if focused == "missing":
                raise ValueError(f"ref {ref!r} no longer resolves — take a fresh snapshot")
        if text:
            await transport.call(
                "Input.insertText",
                session=page.cdp_session,
                text=str(text),
                timeout=float(self.config["op_timeout_s"]),
            )
        if submit:
            await self.press(session_id, "Enter", page_id=page.page_id)
        page.last_used = time.monotonic()
        return {"page": page.page_id, "typed": len(str(text)), "submitted": bool(submit)}

    async def press(self, session_id: str, keys: str, page_id: str | None = None) -> dict[str, Any]:
        """Press one named key (Enter / Tab / Escape / arrows / …)."""
        page = self.page_for(session_id, page_id)
        name = str(keys or "").strip()
        entry = _KEY_TABLE.get(name)
        if entry is None:
            raise ValueError(f"unsupported key {name!r}; supported: {', '.join(_KEY_TABLE)}")
        code, virtual = entry
        transport = self._require_transport()
        for kind in ("keyDown", "keyUp"):
            await transport.call(
                "Input.dispatchKeyEvent",
                session=page.cdp_session,
                type=kind,
                key=name,
                code=code,
                windowsVirtualKeyCode=virtual,
                nativeVirtualKeyCode=virtual,
                timeout=float(self.config["op_timeout_s"]),
            )
        page.last_used = time.monotonic()
        return {"page": page.page_id, "pressed": name}

    async def scroll(
        self,
        session_id: str,
        *,
        delta_y: float = 0,
        delta_x: float = 0,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Scroll the page by a wheel delta at the viewport centre."""
        page = self.page_for(session_id, page_id)
        size = await self._eval_raw(
            page,
            "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
            timeout=float(self.config["op_timeout_s"]),
        )
        try:
            viewport = json.loads(size)
        except (TypeError, ValueError):
            viewport = {"w": 800, "h": 600}
        transport = self._require_transport()
        await transport.call(
            "Input.dispatchMouseEvent",
            session=page.cdp_session,
            type="mouseWheel",
            x=float(viewport.get("w", 800)) / 2,
            y=float(viewport.get("h", 600)) / 2,
            deltaX=float(delta_x),
            deltaY=float(delta_y),
            timeout=float(self.config["op_timeout_s"]),
        )
        page.last_used = time.monotonic()
        return {"page": page.page_id, "scrolled": [float(delta_x), float(delta_y)]}

    async def screenshot(
        self,
        session_id: str,
        *,
        full_page: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Capture a PNG to the scratch dir; return its path, size and dimensions.

        The file (not the bytes) is the answer: the agent views it through the
        existing image path, and C1 keeps page pixels out of the message store.
        """
        page = self.page_for(session_id, page_id)
        transport = self._require_transport()
        captured = await transport.call(
            "Page.captureScreenshot",
            session=page.cdp_session,
            format="png",
            captureBeyondViewport=bool(full_page),
            timeout=float(self.config["op_timeout_s"]),
        )
        png = base64.b64decode(captured["data"])
        limit = int(self.config["screenshot_max_bytes"])
        if len(png) > limit:
            raise ValueError(
                f"screenshot is {len(png)} bytes (limit {limit}) — try full_page=False"
            )
        directory = Path(TEMP_DIR) / "browser" / str(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / f"{page.page_id}-{int(time.time() * 1000)}.png"
        file_path.write_bytes(png)
        page.last_used = time.monotonic()
        size = _png_size(png)
        return {
            "page": page.page_id,
            "path": str(file_path),
            "bytes": len(png),
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "full_page": bool(full_page),
        }

    async def evaluate(
        self, session_id: str, expression: str, page_id: str | None = None
    ) -> dict[str, Any]:
        """Run one JS expression in the page (gated by ``allow_evaluate``).

        :raises PermissionError: the escape hatch is switched off.
        """
        if not self.config["allow_evaluate"]:
            raise PermissionError(
                "browser_evaluate is disabled (set "
                f"{self.config['allow_evaluate_env_var']}=1 to enable it)"
            )
        script = str(expression or "").strip()
        if not script:
            raise ValueError("expression is required")
        page = self.page_for(session_id, page_id)
        raw = await self._eval_raw(page, script, timeout=float(self.config["op_timeout_s"]))
        text = json.dumps(raw, ensure_ascii=False, default=str)
        page.last_used = time.monotonic()
        return {
            "page": page.page_id,
            "result": text[:_EVALUATE_MAX_CHARS],
            "truncated": len(text) > _EVALUATE_MAX_CHARS,
        }

    async def reload(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """Reload a page and wait for the new load (the panel's 刷新)."""
        page = self.page_for(session_id, page_id)
        async with page.nav_lock:
            await self._require_transport().call(
                "Page.reload", session=page.cdp_session, timeout=float(self.config["op_timeout_s"])
            )
            page.refs.clear()
            finished = await self._wait_ready(page, float(self.config["nav_timeout_s"]))
        location = await self._page_location(page)
        page.url = location.get("url") or page.url
        page.title = location.get("title") or ""
        page.last_used = time.monotonic()
        return {**page.info(), "loaded": finished}

    async def page_history(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """The page's own back/forward state (drives the panel's nav buttons)."""
        page = self.page_for(session_id, page_id)
        try:
            history = await self._require_transport().call(
                "Page.getNavigationHistory",
                session=page.cdp_session,
                timeout=float(self.config["op_timeout_s"]),
            )
        except CdpError:
            return {**page.info(), "can_back": False, "can_forward": False}
        entries = history.get("entries") or []
        index = int(history.get("currentIndex") or 0)
        return {
            **page.info(),
            "can_back": index > 0,
            "can_forward": index < len(entries) - 1,
        }

    async def history_step(
        self, session_id: str, step: int, page_id: str | None = None
    ) -> dict[str, Any]:
        """Walk the page's own navigation history by ``step`` entries (+1 / -1)."""
        page = self.page_for(session_id, page_id)
        transport = self._require_transport()
        timeout = float(self.config["op_timeout_s"])
        history = await transport.call(
            "Page.getNavigationHistory", session=page.cdp_session, timeout=timeout
        )
        entries = history.get("entries") or []
        current = int(history.get("currentIndex") or 0)
        target_index = current + int(step)
        if not 0 <= target_index < len(entries):
            return {
                **page.info(),
                "moved": False,
                "can_back": current > 0,
                "can_forward": current < len(entries) - 1,
            }
        async with page.nav_lock:
            await transport.call(
                "Page.navigateToHistoryEntry",
                session=page.cdp_session,
                entryId=entries[target_index]["id"],
                timeout=timeout,
            )
            page.refs.clear()
            finished = await self._wait_ready(page, float(self.config["nav_timeout_s"]))
        location = await self._page_location(page)
        page.url = location.get("url") or page.url
        page.title = location.get("title") or ""
        page.last_used = time.monotonic()
        return {
            **page.info(),
            "moved": True,
            "loaded": finished,
            "can_back": target_index > 0,
            "can_forward": target_index < len(entries) - 1,
        }

    async def open_devtools(self, session_id: str, page_id: str | None = None) -> dict[str, Any]:
        """Open (or reuse) a devtools frontend target for the session's page.

        The frontend is an ordinary target pointed at the page's CDP endpoint;
        the debug port never leaves this process (C3) — the frontend's own URL is
        reported without it, and only its screencast ever reaches a client.
        Chrome 153 accepts this form and ``Target.openDevTools`` alike; the
        target form is used because the panel can then show AND drive it like
        any other page.
        """
        page = self.page_for(session_id, page_id)
        if page.kind != "page":
            raise ValueError("devtools can only be opened for an ordinary page")
        existing = next(
            (
                candidate
                for candidate in self._pages.values()
                if candidate.session_id == session_id
                and candidate.kind == "devtools"
                and candidate.devtools_for == page.target_id
            ),
            None,
        )
        if existing is not None:
            existing.last_used = time.monotonic()
            return existing.info()
        await self.ensure_started()
        if self._port is None:
            raise CdpError("the browser has no debug port (not launched?)")
        transport = self._require_transport()
        timeout = float(self.config["op_timeout_s"])
        frontend = (
            "devtools://devtools/bundled/inspector.html?ws=127.0.0.1:"
            f"{self._port}/devtools/page/{page.target_id}"
        )
        created = await transport.call("Target.createTarget", url=frontend, timeout=timeout)
        attached = await transport.call(
            "Target.attachToTarget", targetId=created["targetId"], flatten=True, timeout=timeout
        )
        self._counter += 1
        devtools_page = BrowserPage(
            page_id=f"p{self._counter}",
            target_id=created["targetId"],
            cdp_session=attached["sessionId"],
            session_id=session_id,
            url="devtools://devtools/bundled/inspector.html",
            title="DevTools",
            kind="devtools",
            devtools_for=page.target_id,
            created_at=time.monotonic(),
            last_used=time.monotonic(),
        )
        await transport.call("Page.enable", session=devtools_page.cdp_session)
        self._pages[devtools_page.page_id] = devtools_page
        return devtools_page.info()

    async def send_input(
        self,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Forward one raw input event from the panel to the page.

        The panel computes coordinates in the page's own CSS pixels (it divides
        by its render scale), so no mapping happens here; every numeric field is
        coerced and every unknown field is dropped.
        """
        page = self.page_for(session_id, page_id)
        transport = self._require_transport()
        timeout = float(self.config["op_timeout_s"])
        if kind == "text":
            text = str(payload.get("text") or "")
            if text:
                await transport.call(
                    "Input.insertText", session=page.cdp_session, text=text, timeout=timeout
                )
        elif kind in _INPUT_KEYS:
            params: dict[str, Any] = {}
            for key in _INPUT_KEYS[kind]:
                if key in payload:
                    params[key] = payload[key]
            if kind == "wheel":
                # CDP insists on BOTH deltas for a wheel event; a client that only
                # scrolls vertically would otherwise be refused outright.
                params["type"] = "mouseWheel"
                params.setdefault("deltaX", 0)
                params.setdefault("deltaY", 0)
            if not params.get("type"):
                raise ValueError(f"input kind {kind!r} needs a 'type'")
            for field in ("x", "y", "deltaX", "deltaY"):
                if field in params:
                    params[field] = float(params[field])
            for field in (
                "clickCount",
                "modifiers",
                "buttons",
                "windowsVirtualKeyCode",
                "nativeVirtualKeyCode",
            ):
                if field in params:
                    params[field] = int(params[field])
            await transport.call(
                "Input.dispatchMouseEvent"
                if kind in ("mouse", "wheel")
                else "Input.dispatchKeyEvent",
                session=page.cdp_session,
                timeout=timeout,
                **params,
            )
        else:
            raise ValueError(f"unknown input kind {kind!r}")
        page.last_used = time.monotonic()
        return {"page": page.page_id, "kind": kind}

    # ------------------------------------------------------------------ helpers

    async def _eval_raw(self, page: BrowserPage, expression: str, *, timeout: float) -> Any:
        """Evaluate one expression and return its JSON value."""
        result = await self._require_transport().call(
            "Runtime.evaluate",
            session=page.cdp_session,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("text") or detail.get("exception", {}).get("description")
            raise CdpError(f"page script failed: {text}")
        return result.get("result", {}).get("value")

    async def _page_location(self, page: BrowserPage) -> dict[str, Any]:
        """The page's current URL and title (best effort — never raises)."""
        try:
            raw = await self._eval_raw(
                page,
                "JSON.stringify({url: location.href, title: document.title})",
                timeout=float(self.config["op_timeout_s"]),
            )
            return json.loads(raw) if isinstance(raw, str) else {}
        except (CdpError, ValueError, TypeError):
            return {}

    async def _wait_ready(self, page: BrowserPage, timeout: float) -> bool:
        """Poll ``document.readyState`` until complete (bounded)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                state = await self._eval_raw(page, "document.readyState", timeout=2.0)
            except CdpError:
                state = None
            if state == "complete":
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _require_transport(self) -> BrowserTransport:
        if self._transport is None or not self._transport.connected:
            raise CdpError("the browser is not running — the feature may be disabled")
        return self._transport


# ------------------------------------------------------------------ singleton

_manager: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    """The process-wide manager (created on first use; lazy launch on first op)."""
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


def reset_browser_manager() -> None:
    """Drop the singleton (tests / process teardown)."""
    global _manager
    _manager = None


def shutdown_browser_blocking(timeout_s: float = 3.0) -> None:
    """Best-effort teardown for interpreter exit (atexit).

    The async :meth:`BrowserManager.shutdown` needs the server's event loop; at
    exit that loop is gone, so this signals the Chromium child directly — the
    exact PID this process spawned, never a pattern match.
    """
    manager = _manager
    if manager is None or manager._pid is None:
        return
    pid = manager._pid
    manager._pid = None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
