"""The local chat daemon: wire contract, client shim and the aux-model wiring.

The daemon is a real loopback server over a fake ``Llama`` (no GGUF); the client
tests fake the shared lifecycle so the payload shape, the instructor-compatible
``ChatCompletion`` and the fallback decision are pinned without loading a model.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
import requests

import models.LLMs.auxiliary_llm.core as aux
import models.LLMs.local_daemon.client as client_module
import models.LLMs.local_daemon.server as server
from models.daemon.base import DaemonError, ReadyState

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeLlama:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 1,
            "model": "local",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hello!",
                        "reasoning_content": "hmm",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _NoopWatchdog:
    def touch(self) -> None:
        pass


@pytest.fixture()
def live_daemon(monkeypatch):
    fake = _FakeLlama()
    monkeypatch.setattr(server, "_model", fake)
    monkeypatch.setattr(server._Handler, "watchdog", _NoopWatchdog(), raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, fake
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_health_reports_ready(live_daemon):
    base, _fake = live_daemon
    assert requests.get(f"{base}/healthy", timeout=5).json() == {"status": "ok", "ready": True}


def test_chat_completion_forwards_the_llama_kwargs(live_daemon):
    base, fake = live_daemon

    response = requests.post(
        f"{base}/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 32,
            "stop": [],
        },
        timeout=5,
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello!"
    assert fake.calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 32,
            "stop": [],
        }
    ]


def test_bad_requests_are_refused_without_reaching_the_model(live_daemon):
    base, fake = live_daemon

    for payload in (
        {"messages": []},
        {"messages": "hi"},
        {"messages": [{"role": "user", "content": "hi"}], "stream": True},
        {"messages": [{"role": "user", "content": "hi"}], "nonsense": 1},
    ):
        assert (
            requests.post(f"{base}/v1/chat/completions", json=payload, timeout=5).status_code == 500
        )

    assert fake.calls == []


def test_unknown_path_is_404(live_daemon):
    base, _fake = live_daemon
    assert requests.post(f"{base}/nope", json={}, timeout=5).status_code == 404


def _wire(monkeypatch, responses):
    state = {"ready": ReadyState.READY}
    calls: list[dict] = []

    monkeypatch.setattr(client_module, "ensure_ready", lambda *a, **k: state["ready"])

    def _request(_spec, _path, payload, *, timeout_s):
        calls.append(payload)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client_module, "request_json", _request)
    return state, calls


def _client() -> client_module.DaemonChatClient:
    return client_module.DaemonChatClient(model_path="/fake.gguf", n_ctx=4096)


def test_client_forwards_only_the_chat_params(monkeypatch):
    chat = _client()
    _state, calls = _wire(
        monkeypatch,
        [{"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]}],
    )

    result = chat.create_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=16,
        stop=[],
        caller_private="drop-me",
    )

    assert result["choices"][0]["message"]["content"] == "hi"
    assert calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.1,
            "max_tokens": 16,
            "stop": [],
        }
    ]


def test_client_openai_v1_returns_a_chat_completion(monkeypatch):
    from openai.types.chat import ChatCompletion

    chat = _client()
    _state, calls = _wire(
        monkeypatch,
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ],
    )

    result = chat.create_chat_completion_openai_v1(
        messages=[{"role": "user", "content": "hi"}],
        response_model=dict,  # instructor's own kwarg must not cross the wire
        response_format={"type": "json_object"},
    )

    assert isinstance(result, ChatCompletion)
    assert result.choices[0].message.content == "hi"
    assert calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        }
    ]


def test_client_retries_once_after_a_daemon_error(monkeypatch):
    chat = _client()
    _state, calls = _wire(
        monkeypatch,
        [
            DaemonError("gone"),
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]},
        ],
    )

    assert (
        chat.create_chat_completion(messages=[{"role": "user", "content": "hi"}])["choices"][0][
            "message"
        ]["content"]
        == "ok"
    )
    assert len(calls) == 2


def test_client_rejects_a_malformed_completion(monkeypatch):
    chat = _client()
    _wire(monkeypatch, [{"nope": True}, {"nope": True}])

    with pytest.raises(DaemonError, match="malformed"):
        chat.create_chat_completion(messages=[{"role": "user", "content": "hi"}])


def test_vision_client_targets_the_vision_daemon(monkeypatch):
    from config.features import LOCAL_DAEMONS

    seen: dict = {}

    def _ensure(spec, **kwargs):
        seen["spec"] = spec
        seen["env"] = kwargs.get("extra_env")
        return ReadyState.READY

    monkeypatch.setattr(client_module, "ensure_ready", _ensure)
    monkeypatch.setattr(
        client_module,
        "request_json",
        lambda *_a, **_k: {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]
        },
    )
    chat = client_module.build_daemon_chat_client(
        model_path="/fake/model.gguf",
        mmproj_path="/fake/mmproj.gguf",
        n_ctx=2048,
        vision=True,
    )

    chat.create_chat_completion(messages=[{"role": "user", "content": "describe"}])

    assert seen["spec"].port == LOCAL_DAEMONS["vision_port"]
    assert seen["env"]["SHERRY_LOCAL_LLM_MMPROJ_PATH"] == "/fake/mmproj.gguf"
    assert seen["env"]["SHERRY_LOCAL_LLM_MODEL_PATH"] == "/fake/model.gguf"
    # The shared server binds the ROLE's port; both chat daemons run the same
    # module, so the child must be told which one it is.
    assert seen["env"]["SHERRY_LOCAL_LLM_DAEMON_PORT"] == str(LOCAL_DAEMONS["vision_port"])


def test_vision_daemon_loads_the_chat_handler(monkeypatch):
    import sys
    import types

    calls: dict = {}

    class _FakeHandler:
        def __init__(self, clip_model_path, verbose):
            calls["clip_model_path"] = clip_model_path

    class _FakeLlama:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    llama_stub = types.ModuleType("llama_cpp")
    fmt_stub = types.ModuleType("llama_cpp.llama_chat_format")
    llama_stub.Llama = _FakeLlama
    fmt_stub.Qwen25VLChatHandler = _FakeHandler
    llama_stub.llama_chat_format = fmt_stub
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_stub)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format", fmt_stub)
    monkeypatch.setenv("SHERRY_LOCAL_LLM_MODEL_PATH", "/fake/model.gguf")
    monkeypatch.setenv("SHERRY_LOCAL_LLM_MMPROJ_PATH", "/fake/mmproj.gguf")
    monkeypatch.setenv("SHERRY_LOCAL_LLM_N_CTX", "2048")
    monkeypatch.setattr(server, "_model", None)

    server._get_model()

    assert calls["clip_model_path"] == "/fake/mmproj.gguf"
    assert calls["n_batch"] == 2048 and calls["n_ubatch"] == 2048
    assert calls["chat_handler"] is not None


def test_multimodal_base_uses_the_vision_daemon(monkeypatch):
    from config.features import LOCAL_DAEMONS
    from models.LLMs.base_local_llama import LocalMultimodalLlamaChatBase

    class _MM(LocalMultimodalLlamaChatBase):
        def _resolve_model_path(self) -> str:
            return "/fake/model.gguf"

        def _resolve_mmproj_path(self) -> str:
            return "/fake/mmproj.gguf"

    built: dict = {}

    def _build(**kwargs):
        built["client"] = _FakeDaemonClient(**kwargs)
        return built["client"]

    monkeypatch.setitem(LOCAL_DAEMONS, "vision_daemon_enabled", True)
    monkeypatch.setattr("models.LLMs.local_daemon.client.build_daemon_chat_client", _build)
    model = _MM(model_path="/fake/model.gguf", mmproj_path="/fake/mmproj.gguf")

    assert model._ensure_client() is built["client"]
    assert built["client"].kwargs["vision"] is True
    assert built["client"].kwargs["mmproj_path"] == "/fake/mmproj.gguf"
    model._release_client()


def test_available_is_false_when_the_daemon_cannot_start(monkeypatch):
    chat = _client()
    state, _calls = _wire(monkeypatch, [])
    state["ready"] = ReadyState.UNAVAILABLE

    assert chat.available() is False


class _FakeDaemonClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ready = True

    def available(self) -> bool:
        return self.ready

    def close(self) -> None:
        pass


def test_local_model_uses_the_daemon_when_available(monkeypatch):
    built: dict[str, _FakeDaemonClient] = {}

    def _build(**kwargs: Any) -> _FakeDaemonClient:
        built["client"] = _FakeDaemonClient(**kwargs)
        return built["client"]

    monkeypatch.setattr("models.LLMs.local_daemon.client.build_daemon_chat_client", _build)
    model = aux._LocalAuxiliaryChatModel(model_path="/fake.gguf", n_ctx=4096)

    assert model._ensure_client() is built["client"]
    assert built["client"].kwargs["model_path"] == "/fake.gguf"
    model._release_client()


def test_local_model_falls_back_to_llama_when_the_daemon_is_unavailable(monkeypatch):
    pytest.importorskip("llama_cpp")
    fake = _FakeDaemonClient()
    fake.ready = False
    monkeypatch.setattr(
        "models.LLMs.local_daemon.client.build_daemon_chat_client", lambda **kwargs: fake
    )
    sentinel = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("llama_cpp.Llama", lambda **kwargs: sentinel)
    model = aux._LocalAuxiliaryChatModel(model_path="/fake.gguf", n_ctx=4096)

    assert model._ensure_client() is sentinel
    model._release_client()


def test_local_branch_uses_the_configured_n_ctx(monkeypatch):
    from config import features

    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _Normalizer:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    monkeypatch.setenv("AUXILIARY_LLM_MODEL_LOCAL", "true")
    monkeypatch.setattr(
        features, "LOCAL_DAEMONS", {**features.LOCAL_DAEMONS, "auxiliary_n_ctx": 8192}
    )
    monkeypatch.setattr(aux, "_LocalAuxiliaryChatModel", _Recorder)
    monkeypatch.setattr(aux, "NormalizingChatModel", _Normalizer)

    aux.build_auxiliary_llm()

    assert captured["n_ctx"] == 8192
    assert captured["max_tokens"] == 4096
