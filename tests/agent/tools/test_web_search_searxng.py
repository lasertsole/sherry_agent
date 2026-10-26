"""SearXNG provider: the request it builds and the result it formats.

Everything here runs against a FAKE ``httpx.Client`` — no instance, no network.
The request's shape is the contract that matters: the JSON API call, the
optional engines/categories passthrough, the proxy, and the TLS toggle are all
asserted on the client that was constructed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import agent.tools.web_search_searxng as searxng

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakeResponse:
    """Minimal httpx.Response stand-in: carries a JSON body or an error."""

    def __init__(self, payload: Any = None, status: int = 200, json_error: bool = False):
        self._payload = payload
        self.status_code = status
        self._json_error = json_error
        self.request = httpx.Request("GET", "http://instance/search")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """Records the client's construction kwargs and the single GET it makes."""

    last_kwargs: dict[str, Any] = {}
    last_get: dict[str, Any] = {}
    response: _FakeResponse | Exception = _FakeResponse({"results": []})

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last_get = {"url": url, **kwargs}
        if isinstance(type(self).response, Exception):
            raise type(self).response
        return type(self).response


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-driven readers: none of the SEARXNG_* vars set unless a test sets one.

    ``WEB_SEARCH_CONFIG`` is the readers' fallback (an import-time snapshot of the
    .env), so it is emptied here too — otherwise a developer whose .env configures
    a real instance would flip the "unconfigured" cases, and CI (no .env) would
    pass while the local run failed.
    """
    for name in (
        "SEARXNG_URL",
        "SEARXNG_PROXY",
        "SEARXNG_ENGINES",
        "SEARXNG_CATEGORIES",
        "SEARXNG_VERIFY_TLS",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, empty in (
        ("searxng_url", ""),
        ("searxng_proxy", ""),
        ("searxng_engines", ""),
        ("searxng_categories", ""),
        ("searxng_verify_tls", True),
    ):
        monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, key, empty)
    monkeypatch.setattr(searxng.httpx, "Client", _FakeClient)
    _FakeClient.last_kwargs = {}
    _FakeClient.last_get = {}
    _FakeClient.response = _FakeResponse({"results": []})


def test_unconfigured_instance_is_reported_without_a_request() -> None:
    assert searxng.searxng_available() is False
    assert "not configured" in searxng.searxng_search("anything")
    assert _FakeClient.last_get == {}, "no request may be attempted without a URL"


def test_status_reports_what_the_panel_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888/")
    monkeypatch.setenv("SEARXNG_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("SEARXNG_ENGINES", "google,bing")
    monkeypatch.setenv("SEARXNG_CATEGORIES", "it")
    monkeypatch.setenv("SEARXNG_VERIFY_TLS", "false")

    status = searxng.searxng_status()

    assert status == {
        "configured": True,
        "url": "http://localhost:8888",  # trailing slash stripped
        "engines": "google,bing",
        "categories": "it",
        "proxy": "http://127.0.0.1:7890",
        "verify_tls": False,
    }
    assert searxng.searxng_available() is True


def test_search_builds_the_json_api_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setenv("SEARXNG_ENGINES", "google,bing")
    monkeypatch.setenv("SEARXNG_CATEGORIES", "it")

    searxng.searxng_search("sherry agent")

    assert _FakeClient.last_get["url"] == "http://localhost:8888/search"
    assert _FakeClient.last_get["params"] == {
        "q": "sherry agent",
        "format": "json",
        "pageno": "1",
        "engines": "google,bing",
        "categories": "it",
    }
    assert _FakeClient.last_get["headers"] == {"Accept": "application/json"}
    # No proxy configured → direct connection, TLS verified, bounded by the timeout.
    assert _FakeClient.last_kwargs["proxy"] is None
    assert _FakeClient.last_kwargs["verify"] is True
    assert _FakeClient.last_kwargs["timeout"] == searxng._TIMEOUT


def test_engines_and_categories_are_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")

    searxng.searxng_search("plain")

    assert set(_FakeClient.last_get["params"]) == {"q", "format", "pageno"}


def test_proxy_and_tls_toggle_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setenv("SEARXNG_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("SEARXNG_VERIFY_TLS", "false")

    searxng.searxng_search("through the proxy")

    assert _FakeClient.last_kwargs["proxy"] == "http://127.0.0.1:7890"
    assert _FakeClient.last_kwargs["verify"] is False


def test_results_are_ranked_by_score_and_formatted_like_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = _FakeResponse(
        {
            "results": [
                {"title": "low", "url": "http://low", "content": "c1", "score": 0.1},
                {"title": "high", "url": "http://high", "content": "c2", "score": 0.9},
                {"title": "mid", "url": "http://mid", "content": "", "score": 0.5},
            ]
        }
    )

    text = searxng.searxng_search("ranked")

    assert text.startswith("SearXNG search results for 'ranked' (3 results):")
    assert text.index("high") < text.index("mid") < text.index("low")
    assert "    URL: http://high" in text
    assert "    c2" in text
    # An entry without a snippet keeps title + URL only (no dangling blank line).
    assert "[3] low" in text
    assert "    URL: http://mid" in text


def test_result_limit_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = _FakeResponse(
        {"results": [{"title": f"r{i}", "url": f"http://{i}", "score": i / 10} for i in range(9)]}
    )

    text = searxng.searxng_search("many", limit=3)

    assert "(3 results)" in text
    # Top three by score (r8, r7, r6); the fourth (r5) is dropped.
    for kept in ("http://8", "http://7", "http://6"):
        assert kept in text
    assert "http://5" not in text


def test_empty_results_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = _FakeResponse({"results": []})

    assert searxng.searxng_search("nothing") == "No results found for 'nothing'."


def test_http_error_names_the_status_and_the_json_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = _FakeResponse(status=403)

    text = searxng.searxng_search("forbidden")

    assert "HTTP 403" in text
    assert "format=json" in text


def test_connection_failure_names_the_proxy_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setenv("SEARXNG_PROXY", "http://127.0.0.1:7890")
    _FakeClient.response = httpx.ConnectError("connection refused")

    text = searxng.searxng_search("unreachable")

    assert "could not reach http://localhost:8888" in text
    assert "via proxy http://127.0.0.1:7890" in text


def test_direct_connection_failure_says_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = httpx.ConnectError("connection refused")

    assert "direct connection" in searxng.searxng_search("unreachable")


def test_a_non_json_body_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    _FakeClient.response = _FakeResponse(json_error=True)

    assert searxng.searxng_search("html error page") == (
        "SearXNG search failed (invalid response — expected JSON)."
    )


def test_the_config_snapshot_is_the_fallback_when_no_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precedence pin: the live env wins, the boot-time ``WEB_SEARCH_CONFIG``
    (the .env the panel writes at boot) answers when the var is absent."""
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_url", "http://from-config:8888/")
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_engines", "from-config-engine")

    assert searxng._searxng_url() == "http://from-config:8888"
    assert searxng.searxng_available() is True
    assert searxng._engines() == "from-config-engine"

    monkeypatch.setenv("SEARXNG_URL", "http://from-env:9999")
    monkeypatch.setenv("SEARXNG_ENGINES", "from-env-engine")

    assert searxng._searxng_url() == "http://from-env:9999"
    assert searxng._engines() == "from-env-engine"


# ── the failure breaker (design concept #21) ─────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_breaker():
    """Process-level breaker state: every test starts and ends closed."""
    searxng.record_searxng_outcome(True)
    yield
    searxng.record_searxng_outcome(True)


def test_the_streak_opens_the_window_at_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)

    searxng.record_searxng_outcome(False)
    searxng.record_searxng_outcome(False)
    assert searxng.searxng_cooling_down() is False
    assert searxng.searxng_failure_streak() == 2

    searxng.record_searxng_outcome(False)
    assert searxng.searxng_cooling_down() is True


def test_a_success_resets_the_streak_and_closes_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_failure_threshold", 3)
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)

    searxng.record_searxng_outcome(False)
    searxng.record_searxng_outcome(False)
    searxng.record_searxng_outcome(True)

    assert searxng.searxng_failure_streak() == 0
    searxng.record_searxng_outcome(False)
    searxng.record_searxng_outcome(False)
    assert searxng.searxng_cooling_down() is False, "the count restarts after a success"


def test_a_zero_threshold_disables_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_failure_threshold", 0)
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)

    for _ in range(10):
        searxng.record_searxng_outcome(False)

    assert searxng.searxng_cooling_down() is False


def test_recovery_is_consumed_once_after_the_window_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_failure_threshold", 1)
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 0.0)

    searxng.record_searxng_outcome(False)

    # A zero-length window is expired the instant it opens: the first consumer
    # observes the expiry (and gets a fresh streak), the second sees nothing.
    assert searxng.consume_searxng_recovery() is True
    assert searxng.searxng_failure_streak() == 0
    assert searxng.consume_searxng_recovery() is False


def test_transport_failures_are_the_only_breaker_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status error or a bad body still REACHED the instance — no timeout tail,
    so it must not open the window (the message already names the real fix)."""
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_failure_threshold", 2)
    monkeypatch.setitem(searxng.WEB_SEARCH_CONFIG, "searxng_cooldown_seconds", 60)
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888/")
    _FakeClient.response = _FakeResponse({"results": []}, status=403)

    for _ in range(5):
        text = searxng.searxng_search("anything")
        assert "HTTP 403" in text

    assert searxng.searxng_cooling_down() is False

    _FakeClient.response = httpx.ConnectError("connection refused")
    text = searxng.searxng_search("anything")
    assert "could not reach" in text
    searxng.record_searxng_outcome(False)
    searxng.record_searxng_outcome(False)
    assert searxng.searxng_cooling_down() is True
