"""web_fetch: one URL in, markdown (or verbatim text) out.

Two branches share one tail: public URLs go through the SSRF guard + DNS pinning
(`safe_fetch`), a local/private address needs the opt-in switch, and both end in
the HTML→markdown step plus the character cap. Every transport failure is text,
never an exception.
"""

from __future__ import annotations

import sys

import pytest

import agent.tools.web.web_fetch as wf
from agent.tools.web.web_fetch import web_fetch
from pub.func.validator.safe_fetch import FetchResult

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_HTML = "<html><body><h1>Title</h1><p>Text</p></body></html>"


def _public_fetch(monkeypatch, result: FetchResult, *, public: bool = True):
    """Route the public branch to a canned FetchResult."""
    calls: list[tuple] = []

    def _safe_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return result

    monkeypatch.setattr(wf, "safe_fetch", _safe_fetch)
    monkeypatch.setattr(wf, "is_public_url", lambda url: public)
    return calls


def _public_fetch_sequence(monkeypatch, results: list[FetchResult], *, public: bool = True):
    """Route the public branch through a canned per-call result sequence."""
    calls: list[tuple] = []

    def _safe_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(wf, "safe_fetch", _safe_fetch)
    monkeypatch.setattr(wf, "is_public_url", lambda url: public)
    monkeypatch.setattr(wf, "_TRANSIENT_RETRY_DELAY_S", 0.0)
    return calls


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200, url: str = "http://localhost:3000/"):
        self.content = content
        self.status_code = status
        self.url = url


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient, capturing how it was constructed."""

    def __init__(self, *, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.kwargs: dict = {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url):
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture()
def local_switch_on(monkeypatch):
    monkeypatch.setenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", "1")


class TestPublicBranch:
    @pytest.mark.asyncio
    async def test_a_public_page_comes_back_as_markdown(self, monkeypatch):
        _public_fetch(
            monkeypatch,
            FetchResult(
                ok=True, body=_HTML.encode(), status=200, final_url="https://example.com/x"
            ),
        )

        out = await web_fetch.coroutine(url="https://example.com/x")

        assert out.startswith("URL: https://example.com/x\nStatus: 200")
        assert "# Title" in out
        assert "Text" in out

    @pytest.mark.asyncio
    async def test_a_non_public_url_is_refused_before_fetching(self, monkeypatch):
        calls = _public_fetch(monkeypatch, FetchResult(ok=True, body=b"x"), public=False)

        out = await web_fetch.coroutine(url="http://internal.example/")

        assert out.startswith("Error:")
        assert "not a public URL" in out
        assert calls == [], "the guard must run before any fetch"

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_reported_with_its_reason(self, monkeypatch):
        _public_fetch(
            monkeypatch,
            FetchResult(ok=False, reason="oversize", final_url="https://example.com/big"),
        )

        out = await web_fetch.coroutine(url="https://example.com/big")

        assert out.startswith("Error: fetch failed (oversize)")
        assert "https://example.com/big" in out

    @pytest.mark.asyncio
    async def test_the_public_path_carries_the_configured_budgets(self, monkeypatch):
        from config.features import TOOLS_TIMEOUTS

        calls = _public_fetch(monkeypatch, FetchResult(ok=True, body=_HTML.encode(), status=200))

        await web_fetch.coroutine(url="https://example.com/")

        _url, kwargs = calls[0]
        assert kwargs["timeout"] == float(TOOLS_TIMEOUTS["web_fetch_timeout_seconds"])
        assert kwargs["max_bytes"] == TOOLS_TIMEOUTS["web_fetch_max_bytes"]

    @pytest.mark.asyncio
    async def test_an_empty_url_is_refused(self):
        assert (await web_fetch.coroutine(url="   ")).startswith("Error:")


class TestTransientRetry:
    """Design concept #20: one bounded retry for transient public-fetch failures."""

    @pytest.mark.asyncio
    async def test_a_network_failure_is_retried_once_and_can_recover(self, monkeypatch):
        calls = _public_fetch_sequence(
            monkeypatch,
            [
                FetchResult(ok=False, reason="network", final_url="https://example.com/x"),
                FetchResult(
                    ok=True, body=_HTML.encode(), status=200, final_url="https://example.com/x"
                ),
            ],
        )

        out = await web_fetch.coroutine(url="https://example.com/x")

        assert len(calls) == 2
        assert out.startswith("URL: https://example.com/x\nStatus: 200")

    @pytest.mark.asyncio
    async def test_two_attempts_are_the_ceiling(self, monkeypatch):
        calls = _public_fetch_sequence(
            monkeypatch,
            [FetchResult(ok=False, reason="network", final_url="https://example.com/x")],
        )

        out = await web_fetch.coroutine(url="https://example.com/x")

        assert len(calls) == 2, "exactly one retry, never a third attempt"
        assert out.startswith("Error: fetch failed (network)")

    @pytest.mark.asyncio
    async def test_a_gateway_status_counts_as_transient(self, monkeypatch):
        calls = _public_fetch_sequence(
            monkeypatch,
            [
                FetchResult(ok=True, body=b"busy", status=503, final_url="https://example.com/x"),
                FetchResult(
                    ok=True, body=_HTML.encode(), status=200, final_url="https://example.com/x"
                ),
            ],
        )

        out = await web_fetch.coroutine(url="https://example.com/x")

        assert len(calls) == 2
        assert "Status: 200" in out

    @pytest.mark.asyncio
    async def test_a_client_error_status_is_not_retried(self, monkeypatch):
        calls = _public_fetch_sequence(
            monkeypatch,
            [FetchResult(ok=True, body=b"gone", status=404, final_url="https://example.com/x")],
        )

        await web_fetch.coroutine(url="https://example.com/x")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_permanent_reasons_are_not_retried(self, monkeypatch):
        for reason in ("oversize", "ssrf", "scheme"):
            calls = _public_fetch(
                monkeypatch, FetchResult(ok=False, reason=reason, final_url="https://example.com/x")
            )

            out = await web_fetch.coroutine(url="https://example.com/x")

            assert len(calls) == 1, reason
            assert out.startswith(f"Error: fetch failed ({reason})")


class TestConversion:
    def test_html_becomes_markdown(self):
        assert wf.html_to_markdown("<h1>Title</h1><p>Text</p>").strip() == "# Title\n\nText"

    def test_scripts_styles_and_noscript_are_dropped(self):
        html = "<body><script>alert(1)</script><style>p{}</style><p>keep</p></body>"

        out = wf.html_to_markdown(html)

        assert "alert(1)" not in out
        assert "p{}" not in out
        assert "keep" in out

    def test_links_survive(self):
        assert "https://example.com/" in wf.html_to_markdown(
            '<p>see <a href="https://example.com/">this</a></p>'
        )

    def test_non_html_bodies_pass_through_verbatim(self):
        payload = '{"items": [{"a": 1, "b": "x & y"}]}'

        assert wf.html_to_markdown(payload) == payload

    def test_a_missing_html2text_degrades_to_tag_stripping(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "html2text", None)

        out = wf.html_to_markdown("<h1>Title</h1><p>Text</p>")

        assert "Title" in out and "Text" in out
        assert "<h1>" not in out


class TestOutputCap:
    @pytest.mark.asyncio
    async def test_an_oversized_page_is_truncated_with_a_notice(self, monkeypatch):
        from config.features import TOOLS_TIMEOUTS

        cap = TOOLS_TIMEOUTS["web_fetch_max_output_chars"]
        _public_fetch(
            monkeypatch,
            FetchResult(ok=True, body=("<p>" + "x" * (cap + 1000) + "</p>").encode(), status=200),
        )

        out = await web_fetch.coroutine(url="https://example.com/long")

        assert f"[... truncated at {cap} chars" in out
        assert len(out) < cap + 500


class TestLocalBranch:
    def test_local_addresses_are_classified_by_literal_address(self):
        for url in (
            "http://localhost:3000",
            "http://LOCALHOST/",
            "http://127.0.0.1:8080/api",
            "http://192.168.1.50",
            "http://10.0.0.1/x",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]:3000",
        ):
            assert wf.is_local_url(url) is True, url

    def test_public_urls_and_hostnames_are_not_local(self):
        for url in ("https://example.com", "http://8.8.8.8/", "ftp://localhost/x", "not a url"):
            assert wf.is_local_url(url) is False, url

    @pytest.mark.asyncio
    async def test_the_switch_off_refuses_and_never_fetches(self, monkeypatch):
        monkeypatch.delenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", raising=False)

        async def _boom(url):
            raise AssertionError("a local fetch must not run while the switch is off")

        monkeypatch.setattr(wf, "_fetch_local", _boom)

        out = await web_fetch.coroutine(url="http://localhost:3000/")

        assert out.startswith("Error:")
        assert "SHERRY_WEB_FETCH_ALLOW_LOCAL=1" in out

    @pytest.mark.asyncio
    async def test_the_switch_on_fetches_locally(self, monkeypatch, local_switch_on):
        async def _local(url):
            return FetchResult(ok=True, body=_HTML.encode(), status=200, final_url=url)

        monkeypatch.setattr(wf, "_fetch_local", _local)

        out = await web_fetch.coroutine(url="http://localhost:3000/")

        assert "# Title" in out
        assert out.startswith("URL: http://localhost:3000/")

    @pytest.mark.asyncio
    async def test_a_public_url_keeps_its_own_path_with_the_switch_on(
        self, monkeypatch, local_switch_on
    ):
        calls = _public_fetch(monkeypatch, FetchResult(ok=True, body=_HTML.encode(), status=200))

        async def _boom(url):
            raise AssertionError("a public URL must not take the local branch")

        monkeypatch.setattr(wf, "_fetch_local", _boom)

        out = await web_fetch.coroutine(url="https://example.com/")

        assert calls and "# Title" in out

    def test_the_switch_reads_the_environment_per_call(self, monkeypatch):
        monkeypatch.setenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", "1")
        assert wf.local_fetch_enabled() is True
        monkeypatch.setenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", "0")
        assert wf.local_fetch_enabled() is False
        monkeypatch.delenv("SHERRY_WEB_FETCH_ALLOW_LOCAL", raising=False)
        assert wf.local_fetch_enabled() is False


class TestLocalFetchTransport:
    @pytest.mark.asyncio
    async def test_a_self_signed_friendly_client_is_used(self, monkeypatch):
        import httpx

        client = _FakeAsyncClient(response=_FakeResponse(_HTML.encode()))
        monkeypatch.setattr(httpx, "AsyncClient", client)

        result = await wf._fetch_local("http://localhost:3000/")

        assert result.ok is True and result.status == 200
        assert client.kwargs["verify"] is False
        assert client.kwargs["follow_redirects"] is True
        assert client.kwargs["max_redirects"] == 5

    @pytest.mark.asyncio
    async def test_a_timeout_is_reported_as_timeout(self, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient(raises=httpx.TimeoutException("too slow")),
        )

        result = await wf._fetch_local("http://localhost:3000/")

        assert result.ok is False
        assert result.reason == "timeout"

    @pytest.mark.asyncio
    async def test_a_connection_error_is_reported(self, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient(raises=httpx.ConnectError("connection refused")),
        )

        result = await wf._fetch_local("http://localhost:3000/")

        assert result.ok is False
        assert "connection refused" in result.reason

    @pytest.mark.asyncio
    async def test_a_body_over_the_byte_cap_is_flagged_oversize(self, monkeypatch):
        import httpx

        from config.features import TOOLS_TIMEOUTS

        cap = TOOLS_TIMEOUTS["web_fetch_max_bytes"]
        monkeypatch.setattr(
            httpx, "AsyncClient", _FakeAsyncClient(response=_FakeResponse(b"x" * (cap + 1)))
        )

        result = await wf._fetch_local("http://localhost:3000/big")

        assert result.ok is False
        assert result.reason == "oversize"
        assert len(result.body or b"") == cap


class TestRegistration:
    def test_the_tool_is_in_the_main_builders_and_the_web_group(self):
        from agent.tools import _MAIN_TOOLS_BUILDERS
        from agent.tools.catalog import TOOL_GROUPS
        from agent.tools.web.web_fetch import build_web_fetch_tool

        # The registry LIST, not ``build_main_tools()``: the subagent test
        # conftest replaces that function with ``lambda: []`` process-wide.
        assert build_web_fetch_tool in _MAIN_TOOLS_BUILDERS
        assert "web_fetch" in TOOL_GROUPS["web"]
        assert build_web_fetch_tool().name == "web_fetch"

    def test_the_tool_declares_its_guardrail_metadata(self):
        built = wf.build_web_fetch_tool()

        assert built.name == "web_fetch"
        assert built.metadata == {"idempotent": False}
        assert built.handle_tool_error is True
