"""Tests for the chat toolbar's context accounting (window / total / parts)."""

import asyncio
import json

import pytest

from server.service import context_usage_service as service
from server.trigger.http import context_usage as http


class _FakeRequest:
    """Minimal Robyn-request double: only ``query_params`` is read."""

    def __init__(self, query_params=None):
        self.query_params = query_params or {}


@pytest.fixture(autouse=True)
def _no_tool_walk(monkeypatch):
    """Tool schemas are a process-static estimate: pin it instead of walking them."""
    monkeypatch.setattr(service, "_tool_schema_tokens", lambda: 2_000)


def test_usage_splits_the_reported_prompt(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 30_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "系统提示词" * 100,
    )

    usage = service.get_context_usage("s1")

    assert usage["window"] == 128_000
    assert usage["total"] == 30_000
    assert usage["tools"] == 2_000
    assert usage["system"] > 0
    # The message part is the remainder of the reported prompt.
    assert usage["messages"] == 30_000 - usage["system"] - 2_000


def test_usage_never_reports_a_negative_message_part(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    # Prompt trimmed to almost nothing while the estimates still add up.
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 100)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "很长的系统提示词" * 500,
    )

    usage = service.get_context_usage("s1")

    assert usage["messages"] == 0
    assert usage["total"] == 100


def test_estimates_are_scaled_to_fit_a_short_prompt(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    # A short prompt (5k reported) against a 2k tool schema and a long system prompt.
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 5_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "系统提示词" * 1_500,
    )

    usage = service.get_context_usage("s1")

    # Both parts shrink together so their sum fits the reported prompt…
    assert usage["system"] + usage["tools"] <= usage["total"]
    # …and the message part takes what is left (nothing here).
    assert usage["messages"] == 5_000 - usage["system"] - usage["tools"]
    assert usage["system"] > usage["tools"]  # proportions survive the scaling


def test_usage_without_a_finished_turn_has_no_total(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 64_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 0)
    monkeypatch.setattr(service, "_system_prompt_text", lambda sid: "")

    usage = service.get_context_usage("s1")

    assert usage == {
        "window": 64_000,
        "total": 0,
        "system": 0,
        "tools": 2_000,
        "messages": 0,
        "cache_hit_ratio": None,
        "compress_ratio": 0.8,
    }


def test_cache_hit_ratio_averages_the_session(monkeypatch):
    class _Row(dict):
        pass

    class _Cursor:
        def fetchone(self):
            return _Row(total=100_000, cached=40_000)

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    assert service._cache_hit_ratio("s1") == 0.4


def test_cache_hit_ratio_is_unknown_without_cached_tokens(monkeypatch):
    class _Cursor:
        def fetchone(self):
            return {"total": 100_000, "cached": None}

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    # Rows written before the column existed, or a provider that never reports
    # cached tokens: the panel must show "unknown", not 0%.
    assert service._cache_hit_ratio("s1") is None


def test_cache_hit_ratio_can_be_a_genuine_zero(monkeypatch):
    class _Cursor:
        def fetchone(self):
            return {"total": 100_000, "cached": 0}

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    # A reported "nothing cached" is 0%, not "unknown".
    assert service._cache_hit_ratio("s1") == 0.0


def test_cache_hit_ratio_survives_a_storage_error(monkeypatch):
    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(service, "get_db", _boom, raising=False)

    assert service._cache_hit_ratio("s1") is None


def test_usage_rejects_a_missing_session():
    with pytest.raises(ValueError):
        service.get_context_usage("")
    with pytest.raises(ValueError):
        service.get_context_usage("   ")


def test_tool_estimate_failure_does_not_break_the_ring(monkeypatch):
    def _boom():
        raise RuntimeError("tool build failed")

    monkeypatch.setattr(service, "_tool_schema_tokens", _boom)
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 5_000)
    monkeypatch.setattr(service, "_system_prompt_text", lambda sid: "")

    usage = service.get_context_usage("s1")

    assert usage["tools"] == 0
    assert usage["messages"] == 5_000


def test_http_handler_returns_the_accounting(monkeypatch):
    monkeypatch.setattr(
        http,
        "get_context_usage",
        lambda sid: {"window": 128_000, "total": 10, "system": 1, "tools": 2, "messages": 7},
    )

    response = asyncio.run(http.context_usage_handler(_FakeRequest({"session_id": "s1"})))

    assert response.status_code == 200
    # `ok()` passes the payload through as the response body.
    payload = json.loads(response.description)
    assert payload["window"] == 128_000
    assert payload["messages"] == 7


def test_http_handler_reports_a_missing_session_id(monkeypatch):
    monkeypatch.setattr(http, "get_context_usage", service.get_context_usage)

    response = asyncio.run(http.context_usage_handler(_FakeRequest({})))

    assert response.status_code == 400
    payload = json.loads(response.description)
    assert payload["success"] is False
    assert "session_id" in payload["message"]
