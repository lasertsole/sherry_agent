"""``web_search``'s backend resolution, Tavily retry → SearXNG fallback, and the
``search_backend_fallback`` frame that tells the client about it.

The Tavily branch is driven through a fake ``langchain_tavily`` (same seam as
``test_web_search_retry.py``); the SearXNG side is monkeypatched at the two
functions the tool imports, so no instance and no socket is needed. Every case
asserts what the MODEL sees (the result string) and what the CLIENT sees (the
push call), which are the two observable contracts of a silent backend swap.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

import agent.tools.web.web_search as web_search
import agent.tools.web.web_search_push as web_search_push

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakeTavily:
    """TavilySearch stand-in: class attributes pick the behaviour, `calls`
    counts the attempts on the INSTANCE (the built tool's own attribute).

    ``envelope`` mirrors the INSTALLED library's own failure shape
    (``return {"error": e}``, verified against langchain-tavily 0.2.18) — the
    path a wrapper that only catches exceptions never sees.
    """

    #: The exception every call raises (``None`` = succeed).
    fail_with: Exception | None = None
    #: An ``{"error": …}`` envelope returned instead of raising.
    envelope: dict[str, Any] | None = None
    #: Never answer (a fresh Event is never set — immune to mocked sleeps).
    hang: bool = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.name = "web_search"
        self.calls = 0

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_with is not None:
            raise self.fail_with
        if self.envelope is not None:
            return dict(self.envelope)
        return "tavily result"


def install_fake_tavily(monkeypatch: pytest.MonkeyPatch, tool_cls: type) -> None:
    """Register a fake ``langchain_tavily`` PACKAGE in ``sys.modules``.

    The submodule matters: the builder subclasses the library's own input model
    (``langchain_tavily.tavily_search.TavilySearchInput``) to declare the injected
    session id, so a fake module that carries only ``TavilySearch`` fails that
    import — the same way a real upstream rename would, which is the point.
    """
    from pydantic import BaseModel, Field

    class _FakeInput(BaseModel):
        query: str = Field(description="Search query to look up")

    package = types.ModuleType("langchain_tavily")
    package.TavilySearch = tool_cls
    submodule = types.ModuleType("langchain_tavily.tavily_search")
    submodule.TavilySearchInput = _FakeInput  # type: ignore[attr-defined]
    package.tavily_search = submodule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_tavily", package)
    monkeypatch.setitem(sys.modules, "langchain_tavily.tavily_search", submodule)


@pytest.fixture
def tavily_branch(monkeypatch: pytest.MonkeyPatch) -> type[_FakeTavily]:
    """The keyed branch over a fake Tavily, with instant backoff."""
    install_fake_tavily(monkeypatch, _FakeTavily)
    monkeypatch.setattr(web_search, "_tavily_api_key", lambda: "tvly-test")
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    _FakeTavily.fail_with = None
    _FakeTavily.envelope = None
    _FakeTavily.hang = False

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(web_search.asyncio, "sleep", _no_sleep)
    return _FakeTavily


@pytest.fixture
def searxng_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """SearXNG available, with its search and the WS pushes both recorded."""
    calls: dict[str, Any] = {"searches": [], "pushes": [], "cooldown_pushes": []}
    monkeypatch.setattr(web_search, "searxng_available", lambda: True)

    def _search(query: str, limit: int = 5) -> tuple[bool, str]:
        calls["searches"].append(query)
        return True, f"searxng result for {query}"

    async def _push(session_id: str, backend: str, reason: str) -> None:
        calls["pushes"].append({"session_id": session_id, "backend": backend, "reason": reason})

    async def _cooldown_push(
        session_id: str, *, opened: bool, cooldown_seconds: float = 0.0
    ) -> None:
        calls["cooldown_pushes"].append(
            {"session_id": session_id, "opened": opened, "cooldown_seconds": cooldown_seconds}
        )

    monkeypatch.setattr(web_search, "searxng_search_outcome", _search)
    monkeypatch.setattr(web_search, "push_search_fallback", _push)
    monkeypatch.setattr(web_search, "push_searxng_cooldown", _cooldown_push)
    return calls


@pytest.fixture(autouse=True)
def _fresh_breaker():
    """The breaker is process-level state: every test starts and ends closed."""
    from agent.tools.web.web_search_searxng import record_searxng_outcome

    record_searxng_outcome(True)
    yield
    record_searxng_outcome(True)


# ── resolution: session choice → global default → availability ───────────────


def test_an_explicit_session_choice_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.session.state_keys import StateKey

    monkeypatch.setattr(
        "runtime.state_register_mem.get_state",
        lambda session_id, key: "searxng" if key == StateKey.SEARCH_BACKEND else None,
    )
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    monkeypatch.setattr(web_search, "searxng_available", lambda: True)

    assert web_search.resolve_preferred_backend("sess-1") == "searxng"


def test_an_explicit_choice_is_ignored_while_its_provider_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session pinned to a provider that lost its configuration must not keep
    aiming at it — the tool would answer "not configured" instead of searching."""
    from runtime.session.state_keys import StateKey

    monkeypatch.setattr(
        "runtime.state_register_mem.get_state",
        lambda session_id, key: "searxng" if key == StateKey.SEARCH_BACKEND else None,
    )
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)

    assert web_search.resolve_preferred_backend("sess-1") == "tavily"


def test_the_default_switches_to_the_only_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_search, "tavily_available", lambda: False)
    monkeypatch.setattr(web_search, "searxng_available", lambda: True)

    assert web_search.resolve_preferred_backend("") == "searxng"


def test_default_searxng_falls_back_to_tavily_when_the_instance_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.features import WEB_SEARCH_CONFIG

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "default_backend", "searxng")
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)

    assert web_search.resolve_preferred_backend("") == "tavily"


# ── the Tavily branch ────────────────────────────────────────────────────────


def test_a_preferred_searxng_skips_tavily_entirely(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "searxng")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert result == "searxng result for query"
    assert tool.calls == 0, "the preferred backend must be the only one called"
    assert searxng_stub["pushes"] == [], "a deliberate choice is not a downgrade"


def test_a_dead_key_falls_back_without_retrying(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.fail_with = RuntimeError("401 Unauthorized: invalid api key")
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query", session_id="sess-1"))

    assert result == "searxng result for query"
    assert tool.calls == 1, "an auth failure cannot be retried into success"
    assert searxng_stub["pushes"] == [
        {"session_id": "sess-1", "backend": "searxng", "reason": "tavily_auth_failed"}
    ]


def test_repeated_failures_end_in_the_fallback_and_one_push(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.fail_with = RuntimeError("upstream hiccup")
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert result == "searxng result for query"
    assert tool.calls == web_search.RETRY_MAX_ATTEMPTS
    assert searxng_stub["pushes"] == [
        {"session_id": "", "backend": "searxng", "reason": "tavily_error"}
    ]


def test_a_timeout_is_reported_as_its_own_reason(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.hang = True
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    monkeypatch.setattr(web_search, "WEB_SEARCH_TIMEOUT", 0.01)
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert result == "searxng result for query"
    assert searxng_stub["pushes"][0]["reason"] == "tavily_timeout"


def test_without_searxng_the_tool_answers_with_the_last_error(
    tavily_branch: type[_FakeTavily], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.fail_with = RuntimeError("still down")
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert "Web search failed after" in result
    assert "still down" in result


def test_a_healthy_tavily_call_returns_itsown_result(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query", session_id="sess-1"))

    assert result == "tavily result"
    assert searxng_stub["pushes"] == []
    assert searxng_stub["searches"] == []


# ── the SearXNG-only branch ──────────────────────────────────────────────────


def test_searxng_only_build_is_a_real_tool_with_metadata(
    monkeypatch: pytest.MonkeyPatch, searxng_stub: dict[str, Any]
) -> None:
    monkeypatch.setattr(web_search, "tavily_available", lambda: False)
    tool = web_search.build_web_search_tool()

    assert tool.name == "web_search"
    assert tool.metadata == {"idempotent": False}
    assert tool.handle_tool_error is True

    result = asyncio.run(tool.ainvoke({"query": "hello"}))

    assert result == "searxng result for hello"
    assert searxng_stub["searches"] == ["hello"]


def test_neither_backend_answers_with_the_configuration_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_search, "tavily_available", lambda: False)
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool.ainvoke({"query": "hello"}))

    assert "neither TAVILY_API_KEY nor SEARXNG_URL" in result
    assert "模型联网" in result


# ── one tool name across every branch ────────────────────────────────────────


def test_every_branch_ships_the_same_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend swap must not show up as a SECOND tool.

    ``TavilySearch``'s own default is ``tavily_search``; the builder pins the
    name to ``web_search`` so the model's tool list, the per-session tool
    selection and the 工具 tab all see one tool whichever provider serves a call.
    """
    # Keyed branch: the REAL library, so the name we would have shipped is visible.
    monkeypatch.setattr(web_search, "_tavily_api_key", lambda: "tvly-test")
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    keyed = web_search.build_web_search_tool()
    assert keyed.name == "web_search"

    # SearXNG-only and stub branches.
    monkeypatch.setattr(web_search, "tavily_available", lambda: False)
    monkeypatch.setattr(web_search, "searxng_available", lambda: True)
    assert web_search.build_web_search_tool().name == "web_search"
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)
    assert web_search.build_web_search_tool().name == "web_search"


# ── the injected session id (per-session choice + notice) ────────────────────


def test_the_session_id_is_injected_through_the_input_schema(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class-based tool never sees the graph state on its own.

    Regression pinned here: ``TavilySearch`` declares no injected argument, so
    ``kwargs["session_id"]`` was ALWAYS empty in production — the per-session
    backend choice and the downgrade notice silently no-op'd (found by the live
    end-to-end run: the tool resolved the global default and pushed an empty
    session id). The fix declares ``InjectedState("session_id")`` on an input
    schema SUBCLASS; both halves are asserted below.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.prebuilt.tool_node import _get_all_injected_args

    # The REAL library here (no fake): this test asserts the schema the ToolNode
    # and the model actually read, so a fake would prove nothing.
    monkeypatch.setattr(web_search, "_tavily_api_key", lambda: "tvly-test")
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    tool = web_search.build_web_search_tool()

    # 1) the model must NOT see the field (tool_call_schema strips injected args)
    model_props = convert_to_openai_tool(tool)["function"]["parameters"]["properties"]
    assert "session_id" not in model_props
    assert "query" in model_props, "the Tavily arguments must survive the subclass"
    # 2) the ToolNode must inject the graph state's session_id into a call
    assert _get_all_injected_args(tool).state.get("session_id") == "session_id"

    # 3) and the value must actually drive the resolution: with the injected id
    # the tool answers from SearXNG and never reaches the (real) Tavily call.
    reached = {"n": 0}

    async def _counting(self, *args, **kwargs):
        reached["n"] += 1
        return "tavily must not be reached"

    monkeypatch.setattr("langchain_tavily.tavily_search.TavilySearch._arun", _counting)
    monkeypatch.setattr(
        web_search,
        "resolve_preferred_backend",
        lambda session_id: "searxng" if session_id == "sess-1" else "tavily",
    )
    result = asyncio.run(tool._arun("query", session_id="sess-1"))

    assert result == "searxng result for query"
    assert reached["n"] == 0, "the injected session id must select the backend before Tavily"
    assert searxng_stub["pushes"] == [], "a deliberate choice is not a downgrade"


# ── the library's error ENVELOPE (returned, not raised) ─────────────────────


def test_an_error_envelope_with_a_dead_key_falls_back_without_retrying(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """langchain-tavily 0.2.x RETURNS ``{"error": e}`` instead of raising.

    Regression pinned here: the wrapper used to only catch exceptions, so a
    401 came back as an envelope the model received verbatim — no retry, no
    fallback, no frame. Live-verified against the real API before the fix.
    """
    tavily_branch.envelope = {"error": Exception("Error 401: Unauthorized")}
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query", session_id="sess-1"))

    assert result == "searxng result for query"
    assert tool.calls == 1, "an auth envelope cannot be retried into success"
    assert searxng_stub["pushes"] == [
        {"session_id": "sess-1", "backend": "searxng", "reason": "tavily_auth_failed"}
    ]


def test_a_generic_error_envelope_is_retried_then_falls_back(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.envelope = {"error": Exception("upstream hiccup")}
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert result == "searxng result for query"
    assert tool.calls == web_search.RETRY_MAX_ATTEMPTS
    assert searxng_stub["pushes"][0]["reason"] == "tavily_error"


def test_a_timeout_envelope_is_reported_as_a_timeout(
    tavily_branch: type[_FakeTavily], searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tavily_branch.envelope = {"error": Exception("HTTPSConnectionPool: read timed out")}
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    tool = web_search.build_web_search_tool()

    asyncio.run(tool._arun("query"))

    assert searxng_stub["pushes"][0]["reason"] == "tavily_timeout"


def test_an_error_envelope_never_reaches_the_model(
    tavily_branch: type[_FakeTavily], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a fallback the answer is the readable summary, NOT ``{'error': …}``."""
    tavily_branch.envelope = {"error": Exception("Error 500: Internal Server Error")}
    monkeypatch.setattr(web_search, "resolve_preferred_backend", lambda session_id: "tavily")
    monkeypatch.setattr(web_search, "searxng_available", lambda: False)
    tool = web_search.build_web_search_tool()

    result = asyncio.run(tool._arun("query"))

    assert "Web search failed after" in result
    assert "Error 500" in result
    assert not isinstance(result, dict)


def test_the_envelope_classifier_ignores_success_payloads(
    tavily_branch: type[_FakeTavily], monkeypatch: pytest.MonkeyPatch
) -> None:
    for payload in (
        "tavily result",
        {"results": [{"title": "hit"}]},
        {"error": ""},
        None,
    ):
        assert web_search._error_envelope(payload) is None, payload
    assert str(web_search._error_envelope({"error": Exception("boom")})) == "boom"
    assert str(web_search._error_envelope({"error": "plain text"})) == "plain text"


# ── the frame itself ─────────────────────────────────────────────────────────


def test_the_push_builds_the_documented_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class _Socket:
        async def send_text(self, text: str) -> None:
            sent.append(text)

    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register,
        "get_websockets_by_session_id",
        lambda session_id: [_Socket()],
    )

    asyncio.run(web_search_push.push_search_fallback("sess-1", "searxng", "tavily_timeout"))

    assert len(sent) == 1
    payload = __import__("json").loads(sent[0])
    assert payload["event"] == "search_backend_fallback"
    assert payload["session_id"] == "sess-1"
    assert payload["content"]["backend"] == "searxng"
    assert payload["content"]["reason"] == "tavily_timeout"
    assert "SearXNG" in payload["content"]["message"]


def test_the_push_is_a_noop_without_a_session_or_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register,
        "get_websockets_by_session_id",
        lambda session_id: calls.append(session_id) or [],
    )

    asyncio.run(web_search_push.push_search_fallback("", "searxng", "tavily_error"))
    asyncio.run(web_search_push.push_search_fallback("sess-1", "searxng", "tavily_error"))

    # The patch is on the SHARED relation register, so an unrelated module's
    # push (a background-task exit watcher left over from another test) can land
    # in this list too — assert the invariants, not an exact call list:
    # an empty session id never reaches the lookup, and the real one does.

    assert "" not in calls, "an empty session id must not even look the socket up"
    assert "sess-1" in calls


def test_a_send_failure_never_reaches_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenSocket:
        async def send_text(self, text: str) -> None:
            raise RuntimeError("socket closed")

    import runtime.session.ws_push as ws_push

    monkeypatch.setattr(
        ws_push.relation_register,
        "get_websockets_by_session_id",
        lambda session_id: [_BrokenSocket()],
    )

    # Must not raise: the search result is already on its way back to the model.
    asyncio.run(web_search_push.push_search_fallback("sess-1", "searxng", "tavily_error"))


# ── the SearXNG failure breaker (design concept #21) ─────────────────────────


def _fail_outcome(query: str, limit: int = 5) -> tuple[bool, str]:
    return False, f"SearXNG search failed (could not reach instance): {query}"


def test_three_transport_failures_open_the_breaker(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import WEB_SEARCH_CONFIG
    from agent.tools.web.web_search_searxng import searxng_cooling_down

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)
    monkeypatch.setattr(web_search, "searxng_search_outcome", _fail_outcome)

    for _ in range(3):
        result = asyncio.run(web_search._search_via_searxng("query", "sess-1"))
    assert "could not reach" in result
    assert searxng_cooling_down() is True
    assert searxng_stub["cooldown_pushes"] == [
        {"session_id": "sess-1", "opened": True, "cooldown_seconds": 60}
    ]


def test_an_open_breaker_skips_the_dial_and_answers_a_stub(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import WEB_SEARCH_CONFIG
    from agent.tools.web.web_search_searxng import record_searxng_outcome

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)
    for _ in range(3):
        record_searxng_outcome(False)
    assert searxng_stub["searches"] == []

    result = asyncio.run(web_search._search_via_searxng("query", "sess-1"))

    assert "temporarily unavailable" in result
    assert searxng_stub["searches"] == [], "a cooling instance must not be dialed"
    assert searxng_stub["cooldown_pushes"] == [], "the stub fires no new transition"


def test_resolution_routes_away_from_a_cooling_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During the cooldown SearXNG reads as unconfigured, so every existing rule
    (explicit choice, default, fallback) takes the other provider."""
    from runtime.session.state_keys import StateKey
    from agent.tools.web.web_search_searxng import record_searxng_outcome

    monkeypatch.setattr(
        "runtime.state_register_mem.get_state",
        lambda session_id, key: "searxng" if key == StateKey.SEARCH_BACKEND else None,
    )
    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    monkeypatch.setattr(web_search, "searxng_available", lambda: True)
    for _ in range(3):
        record_searxng_outcome(False)

    assert web_search.resolve_preferred_backend("sess-1") == "tavily"


def test_an_http_status_error_does_not_open_the_breaker(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import WEB_SEARCH_CONFIG
    from agent.tools.web.web_search_searxng import searxng_cooling_down

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)

    def _status_error(query: str, limit: int = 5) -> tuple[bool, str]:
        return True, "SearXNG search failed (HTTP 403 from instance)."

    monkeypatch.setattr(web_search, "searxng_search_outcome", _status_error)
    for _ in range(5):
        asyncio.run(web_search._search_via_searxng("query", "sess-1"))

    assert searxng_cooling_down() is False
    assert searxng_stub["cooldown_pushes"] == []


def test_the_window_expiring_probes_and_pushes_recovery_on_success(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import WEB_SEARCH_CONFIG
    from agent.tools.web.web_search_searxng import record_searxng_outcome, searxng_cooling_down

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 0.05)
    for _ in range(3):
        record_searxng_outcome(False)
    assert searxng_cooling_down() is True

    import time

    time.sleep(0.06)
    result = asyncio.run(web_search._search_via_searxng("query", "sess-1"))

    assert result == "searxng result for query", "the expiry is probed with a real dial"
    assert searxng_stub["cooldown_pushes"] == [
        {"session_id": "sess-1", "opened": False, "cooldown_seconds": 0.0}
    ]
    assert searxng_cooling_down() is False


def test_a_failed_probe_after_expiry_reopens_only_after_fresh_failures(
    searxng_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.features import WEB_SEARCH_CONFIG
    from agent.tools.web.web_search_searxng import record_searxng_outcome, searxng_cooling_down

    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 0.05)
    for _ in range(3):
        record_searxng_outcome(False)
    import time

    time.sleep(0.06)
    monkeypatch.setattr(web_search, "searxng_search_outcome", _fail_outcome)

    result = asyncio.run(web_search._search_via_searxng("query", "sess-1"))

    assert "could not reach" in result
    assert searxng_cooling_down() is False, "one failure after expiry is not three"
    assert [p["opened"] for p in searxng_stub["cooldown_pushes"]] == []
