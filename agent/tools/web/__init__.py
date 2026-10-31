"""Web tools: search, fetch and their backend helpers."""

from .web_fetch import build_web_fetch_tool as build_web_fetch_tool
from .web_search import (
    build_web_search_tool as build_web_search_tool,
    resolve_preferred_backend as resolve_preferred_backend,
    tavily_available as tavily_available,
)
from .web_search_searxng import searxng_status as searxng_status

__all__ = [
    "build_web_fetch_tool",
    "build_web_search_tool",
    "resolve_preferred_backend",
    "searxng_status",
    "tavily_available",
]
