"""``web_search`` (keyed branch): a real subclass wraps ``_arun`` — with retries.

Regression pinned here: the retry/timeout wrapper used to monkeypatch the
PRIVATE ``TavilySearch._arun``, and the keyed branch returned before the
metadata assignment — so an upstream rename would have silently dropped the
wrapper, and the shipped Tavily tool carried neither ``idempotent`` nor
``handle_tool_error``. The branch now subclasses (an override the type system
checks) and sets the metadata before returning.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

import agent.tools.web_search as web_search

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakeTavily:
    """Minimal TavilySearch stand-in: fails once, then succeeds."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.name = "web_search"
        self.calls = 0

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("upstream hiccup")
        return "search result"


@pytest.fixture
def keyed_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keyed branch with a fake ``langchain_tavily`` and instant backoff."""
    fake_module = types.ModuleType("langchain_tavily")
    fake_module.TavilySearch = _FakeTavily
    monkeypatch.setitem(sys.modules, "langchain_tavily", fake_module)
    # S4: the key is read by a function at BUILD time, not a module global.
    monkeypatch.setattr(web_search, "_tavily_api_key", lambda: "tvly-test")

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(web_search.asyncio, "sleep", _no_sleep)
    return _FakeTavily


def test_keyed_branch_subclasses_and_retries(keyed_env: Any) -> None:
    tool = web_search.build_web_search_tool()

    assert isinstance(tool, keyed_env), "the keyed branch must return a Tavily tool"
    assert tool.metadata == {"idempotent": False}
    assert tool.handle_tool_error is True

    result = asyncio.run(tool._arun("query"))

    assert result == "search result"
    assert tool.calls == 2, "the first failure must be retried"


def test_the_retry_gives_up_with_a_readable_answer(
    keyed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _AlwaysFailing(keyed_env):  # type: ignore[misc,valid-type]
        async def _arun(self, *args: Any, **kwargs: Any) -> str:
            self.calls += 1
            raise RuntimeError("still down")

    fake_module = sys.modules["langchain_tavily"]
    fake_module.TavilySearch = _AlwaysFailing

    tool = web_search.build_web_search_tool()
    result = asyncio.run(tool._arun("query"))

    assert "Web search failed after" in result
    assert "still down" in result
    assert tool.calls == web_search.RETRY_MAX_ATTEMPTS
