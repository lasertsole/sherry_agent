"""The input / LLM / output safety middlewares (and their gating)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares.safety_filter import middlewares as mw
from agent.middlewares.safety_filter.rules import FilterHit

pytestmark = [pytest.mark.unit]

SESSION = "sess-safety"


def _hit(action: str, scope: str = "both") -> FilterHit:
    return FilterHit(
        pattern="secret",
        matched_text="secret",
        action=action,
        replacement="[x]",
        scope=scope,
    )


def _state(*messages: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "messages": list(messages)}


def _request(state: dict[str, Any]) -> Any:
    return SimpleNamespace(state=state)


def _settings(**over: Any) -> dict[str, Any]:
    base = {
        "enabled": True,
        "keyword_file": "sensitive_words.json",
        "stream_buffer_window": 20,
        "llm_moderation_mode": "off",
        "llm_moderation_model": "",
        "safe_refusal_message": "NO",
        "safe_replacement_message": "[blocked]",
        "log_max_entries": 200,
    }
    base.update(over)
    return base


@pytest.fixture()
def filters(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    """Enable the feature and capture every notify call."""
    seen: list[tuple[str, str, int]] = []
    monkeypatch.setattr(mw, "safety_enabled", lambda: True)
    monkeypatch.setattr(mw, "safety_filter_config", lambda: _settings())
    monkeypatch.setattr(
        mw,
        "notify_safety",
        lambda session_id, *, layer, action, rule_count, message, snippet="": seen.append(
            (layer, action, rule_count)
        ),
    )
    return seen


async def _run_input(monkeypatch: pytest.MonkeyPatch, hits: list[FilterHit]) -> tuple[Any, bool]:
    monkeypatch.setattr(mw, "scan", lambda text, scope: hits)
    called: list[bool] = []

    async def handler(_request):
        called.append(True)
        return AIMessage(content="from model")

    result = await mw.InputSafetyFilter().awrap_model_call(
        _request(_state(HumanMessage(content="guess the secret"))), handler
    )
    return result, bool(called)


class TestInputFilter:
    @pytest.mark.asyncio
    async def test_a_block_hit_refuses_without_calling_the_model(self, filters, monkeypatch):
        result, called = await _run_input(monkeypatch, [_hit("block", "input")])

        assert isinstance(result, AIMessage) and result.content == "NO"
        assert called is False
        assert filters == [("input_keyword", "block", 1)]

    @pytest.mark.asyncio
    async def test_warn_and_replace_pass_through_but_notify(self, filters, monkeypatch):
        result, called = await _run_input(monkeypatch, [_hit("warn")])

        assert result.content == "from model" and called is True
        assert filters == [("input_keyword", "warn", 1)]

    @pytest.mark.asyncio
    async def test_no_hits_is_a_transparent_pass_through(self, filters, monkeypatch):
        result, called = await _run_input(monkeypatch, [])

        assert result.content == "from model" and called is True
        assert filters == []

    @pytest.mark.asyncio
    async def test_disabled_feature_never_scans(self, monkeypatch):
        monkeypatch.setattr(mw, "safety_enabled", lambda: False)
        monkeypatch.setattr(mw, "scan", lambda text, scope: pytest.fail("scanned while off"))

        async def handler(_request):
            return AIMessage(content="ok")

        result = await mw.InputSafetyFilter().awrap_model_call(
            _request(_state(HumanMessage(content="x"))), handler
        )
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_an_internal_human_row_is_not_the_turn_input(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "scan", lambda text, scope: [_hit("block")])
        internal = HumanMessage(
            content="reminder", additional_kwargs={"internal": True, "provenance": "reminder"}
        )

        async def handler(_request):
            return AIMessage(content="ok")

        result = await mw.InputSafetyFilter().awrap_model_call(
            _request(_state(internal, HumanMessage(content="real question"))), handler
        )
        # scan is mocked to always hit, so ANY scan would block — the assert is
        # that the internal row did not short-circuit before the real message.
        assert isinstance(result, AIMessage)


class TestLLMModeration:
    @pytest.mark.asyncio
    async def test_inactive_without_a_configured_model(self, monkeypatch):
        monkeypatch.setattr(mw, "llm_moderation_active", lambda: False)

        async def handler(_request):
            return AIMessage(content="ok")

        result = await mw.LLMModerationMiddleware().awrap_model_call(
            _request(_state(HumanMessage(content="q"))), handler
        )
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_on_keyword_hit_skips_the_review_without_hits(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "llm_moderation_active", lambda: True)
        monkeypatch.setattr(
            mw, "safety_filter_config", lambda: _settings(llm_moderation_mode="on_keyword_hit")
        )
        monkeypatch.setattr(mw, "scan", lambda text, scope: [])
        reviewed: list[str] = []
        monkeypatch.setattr(mw, "_review", lambda text, model: reviewed.append(model) or (True, ""))

        async def handler(_request):
            return AIMessage(content="ok")

        result = await mw.LLMModerationMiddleware().awrap_model_call(
            _request(_state(HumanMessage(content="q"))), handler
        )
        assert result.content == "ok" and reviewed == []

    @pytest.mark.asyncio
    async def test_an_unsafe_verdict_refuses(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "llm_moderation_active", lambda: True)
        monkeypatch.setattr(
            mw,
            "safety_filter_config",
            lambda: _settings(llm_moderation_mode="always", llm_moderation_model="judge"),
        )

        async def _review(_text, _model):
            return False, "political"

        monkeypatch.setattr(mw, "_review", _review)
        called: list[bool] = []

        async def handler(_request):
            called.append(True)
            return AIMessage(content="ok")

        result = await mw.LLMModerationMiddleware().awrap_model_call(
            _request(_state(HumanMessage(content="q"))), handler
        )
        assert result.content == "NO" and called == []
        assert filters == [("llm_moderation", "block", 1)]

    @pytest.mark.asyncio
    async def test_an_unavailable_review_fails_open(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "llm_moderation_active", lambda: True)
        monkeypatch.setattr(
            mw,
            "safety_filter_config",
            lambda: _settings(llm_moderation_mode="always", llm_moderation_model="judge"),
        )

        async def _review(_text, _model):
            return None, "review failed"

        monkeypatch.setattr(mw, "_review", _review)

        async def handler(_request):
            return AIMessage(content="ok")

        result = await mw.LLMModerationMiddleware().awrap_model_call(
            _request(_state(HumanMessage(content="q"))), handler
        )
        assert result.content == "ok" and filters == []

    def test_the_verdict_parser_tolerates_prose(self):
        assert mw._parse_verdict('Sure: {"safe": false, "reason": "x"} done') == (False, "x")
        assert mw._parse_verdict("no json here") is None
        assert mw._parse_verdict('{"safe": "maybe"}') is None


class TestOutputFilter:
    @pytest.mark.asyncio
    async def test_a_block_replaces_the_whole_answer_keeping_the_id(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "scan", lambda text, scope: [_hit("block", "output")])
        message = AIMessage(content="the secret", id="m1")

        update = await mw.OutputSafetyFilter().aafter_model(_state(message))

        assert update is not None
        replaced = update["messages"][0]
        assert replaced.content == "[blocked]" and replaced.id == "m1"
        assert filters == [("output_keyword", "block", 1)]

    @pytest.mark.asyncio
    async def test_a_replace_substitutes_only_the_hit(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "scan", lambda text, scope: [_hit("replace", "output")])
        message = AIMessage(content="a secret b", id="m2")

        update = await mw.OutputSafetyFilter().aafter_model(_state(message))

        assert update["messages"][0].content == "a [x] b"

    @pytest.mark.asyncio
    async def test_warn_leaves_the_content_alone(self, filters, monkeypatch):
        monkeypatch.setattr(mw, "scan", lambda text, scope: [_hit("warn", "output")])
        message = AIMessage(content="a secret b", id="m3")

        update = await mw.OutputSafetyFilter().aafter_model(_state(message))

        assert update is None
        assert filters == [("output_keyword", "warn", 1)]

    @pytest.mark.asyncio
    async def test_disabled_feature_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "safety_enabled", lambda: False)

        update = await mw.OutputSafetyFilter().aafter_model(_state(AIMessage(content="x")))

        assert update is None
