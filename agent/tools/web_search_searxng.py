"""SearXNG search provider — self-hosted, no third-party API key.

API contract (the instance's JSON API, enabled by ``search.formats: [json]`` in
its ``settings.yml``)::

    GET {SEARXNG_URL}/search?q=<query>&format=json&pageno=1
    → {"results": [{"title", "url", "content", "score", ...}, ...]}

Adds what the plan calls for beyond a bare request: an httpx proxy (for a
self-hosted instance reached across a MITM'd or filtered network), engine and
category passthrough, and a TLS-verification toggle for MITM proxies.

The output string mirrors the shape Tavily returns (numbered entries with a
title / URL / snippet), so the model sees one tool with one result format and
the backend swap stays invisible to it.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger

from config.features import TOOLS_TIMEOUTS, WEB_SEARCH_CONFIG

__all__ = ["searxng_available", "searxng_search", "searxng_status"]

_TIMEOUT = TOOLS_TIMEOUTS["web_search_timeout_seconds"]

#: The name the tool result names as its source (the client can key on it).
BACKEND_LABEL = "SearXNG"


def _searxng_url() -> str:
    """The instance URL: the live env wins (config is a boot-time snapshot)."""
    return (os.getenv("SEARXNG_URL") or WEB_SEARCH_CONFIG["searxng_url"] or "").strip().rstrip("/")


def _proxy() -> str | None:
    raw = os.getenv("SEARXNG_PROXY") or WEB_SEARCH_CONFIG["searxng_proxy"] or ""
    return raw.strip() or None


def _verify_tls() -> bool:
    raw = os.getenv("SEARXNG_VERIFY_TLS")
    if raw is not None and raw.strip():
        return raw.strip().lower() not in ("0", "false", "no", "off")
    return bool(WEB_SEARCH_CONFIG["searxng_verify_tls"])


def _engines() -> str:
    return (os.getenv("SEARXNG_ENGINES") or WEB_SEARCH_CONFIG["searxng_engines"] or "").strip()


def _categories() -> str:
    return (
        os.getenv("SEARXNG_CATEGORIES") or WEB_SEARCH_CONFIG["searxng_categories"] or ""
    ).strip()


def searxng_available() -> bool:
    """Whether an instance is configured (no network request is made)."""
    return bool(_searxng_url())


def searxng_status() -> dict[str, object]:
    """The panel's read-only view: configured? url / engines / proxy in use."""
    return {
        "configured": searxng_available(),
        "url": _searxng_url(),
        "engines": _engines(),
        "categories": _categories(),
        "proxy": _proxy() or "",
        "verify_tls": _verify_tls(),
    }


def searxng_search(query: str, limit: int = 5) -> str:
    """Search through the configured instance; NEVER raises.

    Failures come back as a readable line naming what went wrong (and the proxy
    in use, so a mis-routed request is diagnosable from the tool result alone).
    """
    base = _searxng_url()
    if not base:
        return "SearXNG search unavailable: SEARXNG_URL is not configured."

    params: dict[str, str] = {"q": query, "format": "json", "pageno": "1"}
    if engines := _engines():
        params["engines"] = engines
    if categories := _categories():
        params["categories"] = categories

    proxy = _proxy()
    try:
        # httpx.Client (not httpx.get): only the client takes proxy + verify +
        # timeout together.
        with httpx.Client(proxy=proxy, verify=_verify_tls(), timeout=_TIMEOUT) as client:
            response = client.get(
                f"{base}/search", params=params, headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("SearXNG HTTP {}: query={}", exc.response.status_code, query)
        return (
            f"SearXNG search failed (HTTP {exc.response.status_code} from {base}). "
            "Check that the instance allows format=json (search.formats in settings.yml)."
        )
    except httpx.RequestError as exc:
        where = f"via proxy {proxy}" if proxy else "direct connection"
        logger.warning("SearXNG unreachable ({}): {}", where, exc)
        return f"SearXNG search failed (could not reach {base}, {where}): {exc}"
    except Exception:
        logger.warning("SearXNG response parse failed", exc_info=True)
        return "SearXNG search failed (invalid response — expected JSON)."

    raw = data.get("results", []) if isinstance(data, dict) else []
    ranked = sorted(raw, key=lambda item: float(item.get("score", 0) or 0), reverse=True)[:limit]
    if not ranked:
        return f"No results found for '{query}'."

    lines = [f"SearXNG search results for '{query}' ({len(ranked)} results):", ""]
    for index, item in enumerate(ranked, start=1):
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        content = str(item.get("content", "")).strip()
        lines.append(f"[{index}] {title}")
        lines.append(f"    URL: {url}")
        if content:
            lines.append(f"    {content}")
        lines.append("")
    return "\n".join(lines)
