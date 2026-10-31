"""Web-search backend selection: which provider serves ``web_search``.

Two providers exist — Tavily (paid API, key in the gitignored ``.env``) and a
self-hosted SearXNG instance (free, no third-party key). The per-session choice
lives in the state register (``StateKey.SEARCH_BACKEND``); this object carries
the GLOBAL connection settings, which do not vary per session.
"""

import os
from collections.abc import Mapping
from typing import TypedDict


class WebSearchConfig(TypedDict):
    """Web-search backend selection: which provider serves ``web_search``."""

    #: "tavily" | "searxng" — the fallback when a session has no explicit choice.
    #: Availability still outranks it: a configured-but-keyless Tavily falls to a
    #: configured SearXNG (and vice versa), so "no provider at all" is the only
    #: state that leaves the tool unavailable.
    default_backend: str
    #: SearXNG base URL (empty = not configured).
    searxng_url: str
    #: httpx proxy for reaching the instance (e.g. "http://127.0.0.1:7890").
    searxng_proxy: str
    #: Comma-separated engine list (empty = the instance's own default set).
    searxng_engines: str
    #: Search category (empty = "general", the instance's default).
    searxng_categories: str
    #: TLS verification — turn OFF only behind a MITM proxy (Clash/V2Ray).
    searxng_verify_tls: bool
    #: Design concept #21 (circuit breaker): consecutive TRANSPORT failures
    #: (timeout / connection error — an HTTP status answer does not count) that
    #: open the SearXNG cooldown. 0 disables the breaker entirely.
    searxng_failure_threshold: int
    #: How long the cooldown lasts, in seconds, once the threshold is reached.
    searxng_cooldown_seconds: float


def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _bounded_int(raw: str, default: int, floor: int) -> int:
    try:
        return max(floor, int(raw))
    except ValueError:
        return default


def _bounded_float(raw: str, default: float, floor: float) -> float:
    try:
        return max(floor, float(raw))
    except ValueError:
        return default


def build_web_search_config(env: Mapping[str, str] | None = None) -> WebSearchConfig:
    """Build the settings, reading the environment at call time.

    Env-sourced like the browser feature's switches: the instance URL and the
    default backend come from the ``.env`` (written by the 模型联网 panel), and
    an unavailable-name default degrades to ``"tavily"`` rather than shipping an
    unknown backend name into the resolver.
    """
    source = env if env is not None else os.environ

    def raw(name: str, default: str = "") -> str:
        return str(source.get(name, default) or "").strip()

    default_backend = raw("WEB_SEARCH_DEFAULT_BACKEND", "tavily").lower() or "tavily"
    if default_backend not in ("tavily", "searxng"):
        default_backend = "tavily"
    return {
        "default_backend": default_backend,
        "searxng_url": raw("SEARXNG_URL"),
        "searxng_proxy": raw("SEARXNG_PROXY"),
        "searxng_engines": raw("SEARXNG_ENGINES"),
        "searxng_categories": raw("SEARXNG_CATEGORIES"),
        "searxng_verify_tls": _truthy(raw("SEARXNG_VERIFY_TLS", "true")),
        "searxng_failure_threshold": _bounded_int(
            raw("WEB_SEARCH_SEARXNG_FAILURE_THRESHOLD", "3"), 3, 0
        ),
        "searxng_cooldown_seconds": _bounded_float(
            raw("WEB_SEARCH_SEARXNG_COOLDOWN_SECONDS", "60"), 60.0, 0.0
        ),
    }


WEB_SEARCH_CONFIG: WebSearchConfig = build_web_search_config()
