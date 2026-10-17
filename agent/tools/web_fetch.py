"""``web_fetch`` — read one URL and hand its content back as markdown.

The companion to ``web_search``: search finds URLs, this reads one. Public
http(s) URLs go through the SSRF guard + DNS pinning already used by the media
pipeline (``pub.func.validator``); a LOCAL/private URL is a separate, opt-in
branch (``SHERRY_WEB_FETCH_ALLOW_LOCAL=1``) so the agent can look at a dev
server it is debugging (``http://localhost:3000``) without weakening the public
path for everyone else.

Both branches share the tail: a byte cap, HTML→markdown (``html2text``, with a
tag-stripping fallback when the package is missing), and a character cap. The
context eviction layer is the second net — it offloads any tool result over its
own threshold, so an oversized page never floods the window.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, tool
from loguru import logger

from config.features import TOOLS_TIMEOUTS
from pub.func.validator import is_public_url
from pub.func.validator.safe_fetch import FetchResult, safe_fetch

#: Best-effort HTML sniff for the conversion step: a body that carries none of
#: these tags (JSON, plain text, a log file) is passed through VERBATIM —
#: html2text would escape its entities and reflow it into garbage.
_HTML_TAG_RE = re.compile(
    r"<(?:!doctype\s+html|html|head|body|div|p|span|a\b|table|ul|ol|li|h[1-6])\b",
    re.IGNORECASE,
)
_STRIP_FIRST_RE = re.compile(
    r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)


def local_fetch_enabled() -> bool:
    """Whether the local/private URL branch is switched on for this process.

    Read per call (not at import) so a test — or a `.env` reload — takes effect
    without reimporting the module.
    """
    return os.getenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", "0").strip() == "1"


def is_local_url(url: str) -> bool:
    """True when *url* is an http(s) URL pointing at a literal local/private address.

    Only literal IPs and ``localhost`` are recognised — no DNS resolution. A
    hostname that RESOLVES to 127.0.0.1 is not caught here; it falls through to
    the public path, where ``is_public_url``'s own resolution refuses it. So the
    switch cannot turn a public-looking hostname into a local fetch.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        # A non-http(s) scheme is a BAD URL, not a local one: it belongs on the
        # public path, whose own scheme check produces the honest refusal.
        return False
    host = parsed.hostname or ""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def html_to_markdown(text: str) -> str:
    """HTML → markdown; a non-HTML body is returned unchanged."""
    if not _HTML_TAG_RE.search(text):
        return text
    cleaned = _STRIP_FIRST_RE.sub("", text)
    try:
        import html2text
    except ImportError:  # pragma: no cover - the package ships in the lockfile
        logger.warning("web_fetch: html2text unavailable; stripping tags instead")
        return re.sub(r"<[^>]+>", "", cleaned)
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0  # never hard-wrap: the model reads the whole line
    return converter.handle(cleaned)


async def _fetch_local(url: str) -> FetchResult:
    """GET a local URL with httpx (the switch is already on).

    No DNS pinning here on purpose: rebinding is a remote-host problem, and the
    local branch exists to reach the operator's own services. Redirects are
    followed with a hop cap, and TLS verification is off — a dev server very
    often serves a self-signed certificate.
    """
    import httpx

    max_bytes = TOOLS_TIMEOUTS["web_fetch_max_bytes"]
    try:
        async with httpx.AsyncClient(
            timeout=float(TOOLS_TIMEOUTS["web_fetch_local_timeout_seconds"]),
            follow_redirects=True,
            max_redirects=5,
            # The opt-in local branch reaches only private/loopback addresses,
            # whose dev services commonly serve a self-signed certificate; the
            # operator asked for this surface with SHERRY_WEB_FETCH_ALLOW_LOCAL=1.
            verify=False,  # nosec B501
        ) as client:
            response = await client.get(url)
            body = response.content
            if len(body) > max_bytes:
                return FetchResult(
                    ok=False,
                    body=body[:max_bytes],
                    declared_length=len(response.content),
                    observed_bytes=max_bytes,
                    reason="oversize",
                    final_url=str(response.url),
                    status=response.status_code,
                )
            return FetchResult(
                ok=True,
                body=body,
                observed_bytes=len(body),
                final_url=str(response.url),
                status=response.status_code,
            )
    except Exception as exc:  # noqa: BLE001 - every transport failure is text
        reason = "timeout" if "timeout" in type(exc).__name__.lower() else str(exc)
        logger.info("web_fetch: local fetch failed url={} reason={}", url, reason)
        return FetchResult(ok=False, reason=reason, final_url=url)


@tool("web_fetch")
async def web_fetch(url: str) -> str:
    """Fetch one http(s) URL and return its content as markdown.

    Use it to read a page a search turned up, or an API endpoint's response.
    Public URLs only by default (SSRF-guarded, DNS-pinned, redirects re-checked);
    a local/private address needs SHERRY_WEB_FETCH_ALLOW_LOCAL=1. Non-HTML bodies
    (JSON, plain text) come back verbatim. The output is truncated — for a very
    large page, fetch the specific sub-page or endpoint you need.
    """
    url = (url or "").strip()
    if not url:
        return "Error: url is required"

    if is_local_url(url):
        if not local_fetch_enabled():
            return (
                f"Error: URL '{url}' is a local/private address. Fetching those is "
                "opt-in: set SHERRY_WEB_FETCH_ALLOW_LOCAL=1 in the environment "
                "configuration and restart the backend."
            )
        result = await _fetch_local(url)
    else:
        if not is_public_url(url):
            return f"Error: URL '{url}' is not a public URL or could not be resolved."
        result = await asyncio.to_thread(
            safe_fetch,
            url,
            timeout=float(TOOLS_TIMEOUTS["web_fetch_timeout_seconds"]),
            max_bytes=TOOLS_TIMEOUTS["web_fetch_max_bytes"],
        )

    if not result.ok:
        return f"Error: fetch failed ({result.reason}). URL: {result.final_url or url}"

    body = result.body or b""
    text = body.decode("utf-8", errors="replace")
    content = html_to_markdown(text)

    max_chars = TOOLS_TIMEOUTS["web_fetch_max_output_chars"]
    if len(content) > max_chars:
        content = (
            f"{content[:max_chars]}\n\n"
            f"[... truncated at {max_chars} chars; the page was {len(content)} chars. "
            "Fetch a more specific URL for the rest.]"
        )
    return f"URL: {result.final_url or url}\nStatus: {result.status}\n\n{content}"


def build_web_fetch_tool() -> BaseTool:
    """Build the ``web_fetch`` tool (registered in ``_MAIN_TOOLS_BUILDERS``)."""
    web_fetch.handle_tool_error = True
    web_fetch.metadata = {"idempotent": False}
    return web_fetch


__all__ = [
    "build_web_fetch_tool",
    "html_to_markdown",
    "is_local_url",
    "local_fetch_enabled",
    "web_fetch",
]
