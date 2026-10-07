"""The env-config connectivity probe (the 环境配置 panels' 测试 button).

The probe must exercise the real clients — that is its whole point — so these
tests stub only the OUTERMOST seam per path (the chat builder, the HTTP client,
the reranker), and pin the contract the panel depends on: a refused probe is a
RESULT (``ok: False`` + the provider's own words), never an exception, and the
groups whose probe would need a media payload say so instead of pretending.
"""

from __future__ import annotations

import asyncio

import pytest

from server.service import model_test_service as service

pytestmark = [pytest.mark.unit]


def _chat_params(**overrides: str) -> dict[str, str]:
    params = {
        "MAIN_LLM_PROVIDER": "openai",
        "MAIN_LLM_API_BASE": "https://gateway.example/v1",
        "MAIN_LLM_API_KEY": "sk-test",
        "MAIN_LLM_NAME": "glm-4.6",
        "MAIN_LLM_MAX_TOKEN": "131072",
    }
    params.update(overrides)
    return params


def test_chat_group_probes_through_the_app_builder(monkeypatch):
    captured: dict[str, object] = {}

    class _Reply:
        content = "pong"

    class _Llm:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return _Reply()

    def fake_builder(**kwargs):
        captured["builder"] = kwargs
        return _Llm()

    monkeypatch.setattr("models.LLMs.main_llm.build_main_llm_for_profile", fake_builder)

    result = asyncio.run(service.test_model_group("MAIN_LLM", _chat_params()))

    assert result["supported"] is True
    assert result["ok"] is True
    assert result["detail"] == "pong"
    assert result["model"] == "glm-4.6"
    assert result["latency_ms"] >= 0
    # The probe used the panel's own (unapplied) parameters, not the env's.
    assert captured["builder"] == {
        "provider": "openai",
        "model": "glm-4.6",
        "api_key": "sk-test",
        "base_url": "https://gateway.example/v1",
    }


def test_chat_group_reports_a_missing_model_name_without_building(monkeypatch):
    def exploding_builder(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the builder must not run without a model name")

    monkeypatch.setattr("models.LLMs.main_llm.build_main_llm_for_profile", exploding_builder)

    result = asyncio.run(service.test_model_group("MAIN_LLM", _chat_params(MAIN_LLM_NAME="")))

    assert result["ok"] is False
    assert "model name" in result["detail"]


def test_a_provider_error_is_the_detail_not_a_raise(monkeypatch):
    def failing_builder(**_kwargs):
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr("models.LLMs.main_llm.build_main_llm_for_profile", failing_builder)

    result = asyncio.run(service.test_model_group("AUXILIARY_LLM", {"AUXILIARY_LLM_API_NAME": "m"}))

    assert result["ok"] is False
    assert "401 invalid api key" in result["detail"]
    assert result["model"] == "m"


def test_a_hung_probe_times_out_instead_of_holding_the_request(monkeypatch):
    class _Llm:
        async def ainvoke(self, messages):
            await asyncio.sleep(60)

    monkeypatch.setattr("models.LLMs.main_llm.build_main_llm_for_profile", lambda **_kwargs: _Llm())
    monkeypatch.setattr(service, "_PING_TIMEOUT_S", 0.05)

    result = asyncio.run(service.test_model_group("MAIN_LLM", _chat_params()))

    assert result["ok"] is False
    assert "no answer within" in result["detail"]


def test_embedding_probe_posts_one_input_and_reports_the_dimension(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    class _Client:
        def __init__(self, base_url: str, api_key: str) -> None:
            calls.append(("init", base_url, {"key": api_key}))

        def post_json(self, path: str, payload: dict) -> dict:
            calls.append(("post", path, payload))
            return {"data": [{"index": 0, "embedding": [0.0] * 1024}]}

    monkeypatch.setattr("models.http_client.OpenAICompatibleClient", _Client)

    result = asyncio.run(
        service.test_model_group(
            "EMBEDDING",
            {
                "EMBEDDING_MODEL_PROVIDER": "openai",
                "EMBEDDING_API_NAME": "bge-m3",
                "EMBEDDING_API_BASE": "https://gw.example/v1",
                "EMBEDDING_API_KEY": "sk-e",
            },
        )
    )

    assert result["ok"] is True
    assert result["detail"] == "embedding vector of 1024 dimensions"
    assert calls[1] == (
        "post",
        "/embeddings",
        {"model": "bge-m3", "input": ["ping"], "encoding_format": "float"},
    )


def test_embedding_probe_needs_the_credentials(monkeypatch):
    result = asyncio.run(service.test_model_group("EMBEDDING", {"EMBEDDING_API_NAME": "bge-m3"}))

    assert result["ok"] is False
    assert "api base" in result["detail"]


def test_reranker_probe_scores_one_pair(monkeypatch):
    import importlib

    reranker_core = importlib.import_module("models.reranker_model.core")

    class _Reranker:
        def __init__(self, api_base: str, api_key: str, model: str | None) -> None:
            assert api_base == "https://gw.example/v1"
            assert model == "bge-reranker-v2-m3"

        def predict(self, query: str, passage: str) -> float:
            assert (query, passage) == ("ping", "pong")
            return 0.8123

    monkeypatch.setattr(reranker_core, "CloudReranker", _Reranker)

    result = asyncio.run(
        service.test_model_group(
            "RERANKER",
            {
                "RERANKER_MODEL_PROVIDER": "openai",
                "RERANKER_API_NAME": "bge-reranker-v2-m3",
                "RERANKER_API_BASE": "https://gw.example/v1",
                "RERANKER_API_KEY": "sk-r",
            },
        )
    )

    assert result["ok"] is True
    assert result["detail"] == "relevance_score=0.8123"


@pytest.mark.parametrize("group", ["ITTT", "VTTT", "TTI", "STT", "OTHER"])
def test_media_and_unknown_groups_answer_unsupported(group):
    result = asyncio.run(service.test_model_group(group, {}))

    assert result["supported"] is False
    assert result["ok"] is False
    assert result["detail"]


def test_the_route_serves_the_probe_result(monkeypatch):
    from server.trigger.http import model_config as http

    class _Request:
        def __init__(self, body):
            self.body = body

    async def fake_test(group: str, params: dict[str, str]) -> dict:
        return {"supported": True, "ok": True, "latency_ms": 12, "detail": "pong", "model": "m"}

    monkeypatch.setattr("server.service.model_test_service.test_model_group", fake_test)
    monkeypatch.setattr("server.trigger.http.helpers.read_body", lambda request: request.body)

    response = asyncio.run(
        http.model_test_handler(_Request({"group": "MAIN_LLM", "params": {"MAIN_LLM_NAME": "m"}}))
    )

    assert response.status_code == 200
    import json

    payload = json.loads(response.description)
    assert payload["ok"] is True
    assert payload["group"] == "MAIN_LLM"


def test_the_route_refuses_a_malformed_request(monkeypatch):
    from server.trigger.http import model_config as http

    class _Request:
        def __init__(self, body):
            self.body = body

    monkeypatch.setattr("server.trigger.http.helpers.read_body", lambda request: request.body)

    missing_group = asyncio.run(http.model_test_handler(_Request({"params": {}})))
    bad_params = asyncio.run(http.model_test_handler(_Request({"group": "MAIN_LLM", "params": []})))

    assert missing_group.status_code == 400
    assert bad_params.status_code == 400
