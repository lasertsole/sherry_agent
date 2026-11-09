"""Shared plumbing for the browser tool family (internal module).

Every verb is a thin shell over the process-wide ``BrowserManager``: the
manager is resolved through ``runtime/hooks.BROWSER_MANAGER`` (``agent/**``
must not import ``server/**``) and the session arrives through
``InjectedState``. All bounds (page caps, navigation/op timeouts,
element/text/screenshot limits) live in ``BROWSER_AGENT`` and are enforced by
the manager, not here.

Error-text contract: a tool NEVER raises a business error to the LLM — the
``guard`` wrapper turns expected failures into readable ``Error: …`` text.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId

__all__ = [
    "BrowserPageInput",
    "SessionId",
    "validate_navigation_url",
    "validate_page_read_url",
    "withhold_blocked_page",
]

#: Opt-in for local/private browser targets (the app's own dev server, a
#: user's localhost app). Off by default: the browser is a model-controlled
#: fetch channel and must not become an SSRF path around ``web_fetch``.
BROWSER_ALLOW_LOCAL_ENV = "SHERRY_BROWSER_ALLOW_LOCAL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _allow_local_browser() -> bool:
    return os.getenv(BROWSER_ALLOW_LOCAL_ENV, "0").strip().lower() in _TRUTHY


async def validate_navigation_url(url: str) -> str | None:
    """Why *url* may not be opened in the browser, or ``None`` when it may.

    Mirrors the ``web_fetch`` contract: http(s) only, local/private targets
    gated by ``SHERRY_BROWSER_ALLOW_LOCAL=1``, and everything else resolved
    through the fail-closed public-URL guard. DNS resolution runs in a thread
    so a slow resolver cannot stall the browser event loop.
    """
    from agent.tools.web.web_fetch import is_local_url
    from pub.func.validator.public_url import is_public_url

    raw = (url or "").strip()
    if not raw:
        return "Error: url is required"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return f"Error: URL {raw!r} is not a valid URL"
    if parsed.scheme not in ("http", "https"):
        return "Error: only http:// and https:// URLs can be opened in the browser"
    if is_local_url(raw):
        if not _allow_local_browser():
            return (
                f"Error: URL {raw!r} is a local/private address. Opening those is opt-in: "
                f"set {BROWSER_ALLOW_LOCAL_ENV}=1 in the environment configuration and "
                "restart the backend."
            )
        return None
    if not await asyncio.to_thread(is_public_url, raw):
        return f"Error: URL {raw!r} is not a public URL or could not be resolved."
    return None


async def validate_page_read_url(url: str | None) -> str | None:
    """Why *url* must not have its CONTENT read, or ``None`` when it may.

    A page can reach a blocked target without ``browser_navigate``: a public
    page's link, a meta refresh, a script, or a history entry. Snapshot /
    screenshot / evaluate are the exfiltration points, so they re-check where
    the page actually IS (the outcome's fresh URL) before returning anything.
    ``about:blank`` is the neutral parked state and always readable.
    """
    raw = (url or "").strip()
    if not raw or raw == "about:blank" or raw.startswith("chrome-error://"):
        # about:blank is the parked state; chrome-error:// is a FAILED load
        # (Chrome's own interstitial, no fetched content to leak).
        return None
    if raw.startswith(("http://", "https://")):
        return await validate_navigation_url(raw)
    scheme = urlparse(raw).scheme or "non-http"
    return f"Error: the page is on a {scheme} URL; only http(s) content can be read."


async def withhold_blocked_page(
    manager: Any, session_id: str, page_id: str | None, url: str | None
) -> str | None:
    """Validate a page's URL and, on a block, park it before returning the error.

    Parking matters: the tool result is the model's only view, but a later
    screenshot of the same page must not be able to resume reading it. A
    parking failure is swallowed — the withheld result is the security outcome.
    """
    blocked = await validate_page_read_url(url)
    if blocked is None:
        return None
    try:
        await manager.navigate(session_id, "about:blank", page_id)
    except Exception:  # noqa: BLE001 - the refusal is what matters
        logger.debug("browser: failed to park a blocked page", exc_info=True)
    return f"Error: page content withheld: {blocked.removeprefix('Error: ')}"


class BrowserPageInput(BaseModel):
    """The two target fields every browser verb shares.

    ``page`` selects which of the session's pages the verb acts on (omitted =
    the session's current page); ``session_id`` arrives through ``InjectedState``
    and is never LLM-facing. The per-verb schemas subclass this instead of
    re-declaring the pair eight times.
    """

    page: str | None = Field(
        default=None, description="Page id from an earlier call; omit for the session's page."
    )
    session_id: SessionId = ""


#: The subagent tool policy drops these unconditionally (see the family
#: docstring: ZCode parity, the browser stays the main agent's hand).
MAIN_ONLY = {"scope": "main_only", "idempotent": False}


def resolve_manager() -> Any:
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


async def guard(operation) -> Any:
    """Run one manager call, turning expected failures into tool-result text.

    ``RuntimeError`` also covers the server's ``CdpError`` (a ``RuntimeError``
    subclass) — the class itself cannot be imported here: ``agent/**`` must not
    import ``server/**``.

    The value is whatever the operation returned: an error string, a success
    string, or (``browser_screenshot``) a text + image content list.
    """
    try:
        return await operation()
    except KeyError as error:
        return f"Error: {error.args[0] if error.args else error}"
    except (ValueError, PermissionError, RuntimeError) as error:
        return f"Error: {error}"


def page_line(info: dict[str, Any]) -> str:
    """``page p1 · «Title» · https://…`` — the one-line page reference."""
    title = info.get("title") or "(no title)"
    return f"page {info.get('page')} · «{title}» · {info.get('url', '')}"


class BrowserTool(BaseTool):
    """Shared base: every verb is async-only.

    The manager is loop-bound (locks + one WebSocket on the server loop), so a
    sync call must not quietly run on another loop — it fails with a message
    that says where to call from instead. The agent graph invokes tools through
    ``ainvoke``, which uses the overridden ``_arun`` and never touches this.
    """

    def _run(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("browser tools are asynchronous — invoke them through the agent graph")
