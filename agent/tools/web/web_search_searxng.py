"""SearXNG search provider — self-hosted, no third-party API key.

API contract (the instance's JSON API, enabled by ``search.formats: [json]`` in
its ``settings.yml``)::

    GET {SEARXNG_URL}/search?q=<query>&format=json&pageno=1
    → {"results": [{"title", "url", "content", "score", ...}, ...]}

Beyond a bare request it carries the connection options a self-hosted instance
needs: an httpx proxy (for an instance reached across a MITM'd or filtered
network), engine and category passthrough, and a TLS-verification toggle for
MITM proxies.

The output string mirrors the shape Tavily returns (numbered entries with a
title / URL / snippet), so the model sees one tool with one result format and
the backend swap stays invisible to it.
"""

from __future__ import annotations

import os
import time
from typing import NamedTuple

import httpx
from loguru import logger

from config.features import TOOLS_TIMEOUTS, WEB_SEARCH_CONFIG

__all__ = [
    "BACKEND_LABEL",
    "SearxngOutcome",
    "consume_searxng_recovery",
    "record_searxng_outcome",
    "searxng_available",
    "searxng_cooling_down",
    "searxng_failure_streak",
    "searxng_search",
    "searxng_search_outcome",
    "searxng_status",
]

_TIMEOUT = TOOLS_TIMEOUTS["web_search_timeout_seconds"]

#: The name the tool result names as its source (the client can key on it).
BACKEND_LABEL = "SearXNG"


class SearxngOutcome(NamedTuple):
    """One search attempt: the result text plus whether the instance was reachable.

    ``reachable`` is False ONLY when the request never got an HTTP exchange (a
    timeout or connection error — ``httpx.RequestError``). A status error or an
    unparseable body still reached the instance, so it does not feed the
    failure breaker: those cost no timeout tail and mean a different fix.
    """

    reachable: bool
    text: str


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


# ── failure breaker (design concept #21) ─────────────────────────────────────
#
# A dead instance costs every call its full ``web_search_timeout_seconds`` and,
# by design, SearXNG never falls back to Tavily — so the ONLY protection is to
# stop dialing it for a while. Process-level state (like the LLM sticky
# fallback), never a session register: the instance is shared by every session.
#
# The window expires on the clock; the next caller observes the expiry through
# ``consume_searxng_recovery`` (a half-open probe) and re-arms the breaker only
# after fresh consecutive failures.

_failure_streak = 0
_cooldown_deadline = 0.0  # time.monotonic() seconds; 0 = the breaker is closed


def _breaker_settings() -> tuple[int, float]:
    return (
        int(WEB_SEARCH_CONFIG["searxng_failure_threshold"]),
        float(WEB_SEARCH_CONFIG["searxng_cooldown_seconds"]),
    )


def record_searxng_outcome(reachable: bool) -> None:
    """Feed one call's outcome into the breaker (a success closes it outright)."""
    global _failure_streak, _cooldown_deadline
    if reachable:
        _failure_streak = 0
        _cooldown_deadline = 0.0
        return
    _failure_streak += 1
    threshold, cooldown = _breaker_settings()
    if threshold > 0 and _failure_streak >= threshold and _cooldown_deadline <= time.monotonic():
        _cooldown_deadline = time.monotonic() + cooldown
        logger.warning(
            "SearXNG failed {} times in a row; cooling down for {}s", _failure_streak, cooldown
        )


def searxng_cooling_down() -> bool:
    """Whether the breaker is open right now (pure read — no clock mutation)."""
    return _cooldown_deadline > time.monotonic()


def searxng_failure_streak() -> int:
    """The current consecutive transport-failure count."""
    return _failure_streak


def consume_searxng_recovery() -> bool:
    """Observe an expired cooldown: clears it and answers True exactly once.

    The caller that is about to dial SearXNG asks this, so the recovery notice
    rides a real attempt — the window expiring on its own is not proof the
    instance came back.
    """
    global _cooldown_deadline, _failure_streak
    if _cooldown_deadline and _cooldown_deadline <= time.monotonic():
        _cooldown_deadline = 0.0
        _failure_streak = 0
        logger.info("SearXNG cooldown window expired; probing the instance again")
        return True
    return False


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


def searxng_search_outcome(query: str, limit: int = 5) -> SearxngOutcome:
    """Search through the configured instance; NEVER raises.

    Failures come back as a readable line naming what went wrong (and the proxy
    in use, so a mis-routed request is diagnosable from the tool result alone).
    The ``reachable`` half is the breaker's input: False only when the instance
    was never reached.
    """
    base = _searxng_url()
    if not base:
        return SearxngOutcome(
            reachable=True,
            text="SearXNG search unavailable: SEARXNG_URL is not configured.",
        )

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
        return SearxngOutcome(
            reachable=True,
            text=(
                f"SearXNG search failed (HTTP {exc.response.status_code} from {base}). "
                "Check that the instance allows format=json (search.formats in settings.yml)."
            ),
        )
    except httpx.RequestError as exc:
        where = f"via proxy {proxy}" if proxy else "direct connection"
        logger.warning("SearXNG unreachable ({}): {}", where, exc)
        return SearxngOutcome(
            reachable=False,
            text=f"SearXNG search failed (could not reach {base}, {where}): {exc}",
        )
    except Exception:
        logger.warning("SearXNG response parse failed", exc_info=True)
        return SearxngOutcome(
            reachable=True,
            text="SearXNG search failed (invalid response — expected JSON).",
        )

    raw = data.get("results", []) if isinstance(data, dict) else []
    ranked = sorted(raw, key=lambda item: float(item.get("score", 0) or 0), reverse=True)[:limit]
    if not ranked:
        return SearxngOutcome(reachable=True, text=f"No results found for '{query}'.")

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
    return SearxngOutcome(reachable=True, text="\n".join(lines))


def searxng_search(query: str, limit: int = 5) -> str:
    """The result text of one search — outcome recorded into the breaker."""
    outcome = searxng_search_outcome(query, limit)
    record_searxng_outcome(outcome.reachable)
    return outcome.text
