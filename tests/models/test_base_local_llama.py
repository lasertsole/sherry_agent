"""TDD tests for audit 1.1.1-1.1.3 — LocalLlamaChatModel Template Method.

Creates ``models/LLMs/base_local_llama.py`` with:

* ``LocalLlamaChatBase(BaseChatModel)`` — common fields, ``_release_client``,
  ``_generate`` skeleton (message conversion + reasoning-extraction flag),
  ``_identifying_params``, ``lc_attributes``.
* ``LocalMultimodalLlamaChatBase`` — adds ``mmproj_path`` and the
  Qwen25VLChatHandler ``_ensure_client`` shared by ITTT/VTTT.

The three per-model ``LocalLlamaChatModel`` classes then only override the
hooks (``_ensure_client`` / ``_convert_message_to_dict`` / resolvers /
``_llm_type``). Behavior pinned with a fake client — no GGUF download.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult

from models.LLMs.base_local_llama import (
    LocalLlamaChatBase,
    LocalMultimodalLlamaChatBase,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60), pytest.mark.module]
FAKE_RESPONSE = {
    "choices": [
        {
            "message": {
                "content": "hello!",
                "reasoning_content": "thinking...",
            }
        }
    ]
}


class _FakeClient:
    def __init__(self, response: dict | None = None):
        self.response = response or FAKE_RESPONSE
        self.calls: list[dict] = []
        self.closed = False

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def close(self):
        self.closed = True


class _TextModel(LocalLlamaChatBase):
    """Minimal concrete subclass mirroring auxiliary_llm's shape."""

    extract_reasoning: bool = True

    def _resolve_model_path(self) -> str:
        return "/fake/model.gguf"

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = _FakeClient()
        return self._client


class _NoReasoningModel(LocalLlamaChatBase):
    extract_reasoning: bool = False

    def _resolve_model_path(self) -> str:
        return "/fake/model.gguf"

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = _FakeClient()
        return self._client


def _make(model_cls, **kwargs):
    return model_cls(model_path="/fake/model.gguf", **kwargs)


class TestBaseClass:
    def test_generate_returns_chat_result(self):
        model = _make(_TextModel)
        result = model._generate([HumanMessage(content="hi")])
        assert isinstance(result, ChatResult)
        assert result.generations[0].message.content == "hello!"

    def test_reasoning_extracted_when_flag_on(self):
        model = _make(_TextModel)
        result = model._generate([HumanMessage(content="hi")])
        msg = result.generations[0].message
        assert msg.additional_kwargs.get("reasoning_content") == "thinking..."

    def test_reasoning_ignored_when_flag_off(self):
        model = _make(_NoReasoningModel)
        result = model._generate([HumanMessage(content="hi")])
        msg = result.generations[0].message
        assert "reasoning_content" not in msg.additional_kwargs

    def test_generate_params_forwarded(self):
        model = _make(_TextModel)
        fake = model._ensure_client()  # capture before generate
        model._generate(
            [HumanMessage(content="hi")], stop=["STOP"], temperature=0.7, max_tokens=128
        )
        call = fake.calls[0]
        assert call["stop"] == ["STOP"]
        assert call["temperature"] == 0.7
        assert call["max_tokens"] == 128

    def test_client_released_after_generate(self):
        model = _make(_TextModel)
        fake = model._ensure_client()  # capture before generate
        model._generate([HumanMessage(content="hi")])
        assert fake.closed is True
        assert model._client is None

    def test_convert_message_roles(self):
        model = _make(_TextModel)
        assert model._convert_message_to_dict(HumanMessage(content="a")) == {
            "role": "user",
            "content": "a",
        }
        assert model._convert_message_to_dict(AIMessage(content="b")) == {
            "role": "assistant",
            "content": "b",
        }
        assert model._convert_message_to_dict(SystemMessage(content="c")) == {
            "role": "system",
            "content": "c",
        }

    def test_identifying_params_shape(self):
        model = _make(_TextModel, n_ctx=1234)
        params = model._identifying_params
        assert params["model_path"] == "/fake/model.gguf"
        assert params["n_ctx"] == 1234

    def test_llm_type_abstract(self):
        with pytest.raises(NotImplementedError):
            _make(LocalLlamaChatBase)._llm_type


class TestMultimodalBase:
    def test_multimodal_client_uses_chat_handler(self, monkeypatch):
        """ITTT/VTTT share the Qwen25VLChatHandler _ensure_client."""

        calls = {}

        class _FakeHandler:
            def __init__(self, clip_model_path, verbose):
                calls["clip_model_path"] = clip_model_path

        class _FakeLlama:
            def __init__(self, **kwargs):
                calls.update(kwargs)

            def close(self):
                pass

        # _ensure_client lazy-imports from llama_cpp — inject stub modules so
        # this passes where llama-cpp-python isn't installed (hermetic CI).
        import sys
        import types

        llama_stub = types.ModuleType("llama_cpp")
        fmt_stub = types.ModuleType("llama_cpp.llama_chat_format")
        llama_stub.Llama = _FakeLlama
        fmt_stub.Qwen25VLChatHandler = _FakeHandler
        llama_stub.llama_chat_format = fmt_stub
        monkeypatch.setitem(sys.modules, "llama_cpp", llama_stub)
        monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format", fmt_stub)

        class _MM(LocalMultimodalLlamaChatBase):
            def _resolve_model_path(self) -> str:
                return "/fake/model.gguf"

            def _resolve_mmproj_path(self) -> str:
                return "/fake/mmproj.bin"

        model = _MM(model_path="/fake/model.gguf", mmproj_path="/fake/mmproj.bin")
        model._ensure_client()
        assert calls["clip_model_path"] == "/fake/mmproj.bin"
        assert calls["chat_handler"] is not None
        assert calls["n_batch"] == model.n_ctx


# ---------------------------------------------------------------------------
# 1.1.3 resolve_gguf_path (parameterized weight resolver)
# ---------------------------------------------------------------------------


class TestResolveGgufPath:
    def test_local_hit_returns_immediately(self, tmp_path, monkeypatch):
        from models.utils import resolve_gguf_path

        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"x")
        called = {"dl": False}

        def _no_download(**kwargs):
            called["dl"] = True

        import huggingface_hub

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", _no_download)
        result = resolve_gguf_path(gguf, "repo/x", "m.gguf", tmp_path)
        assert result == str(gguf)
        assert called["dl"] is False

    def test_fallback_copy(self, tmp_path, monkeypatch):
        from models.utils import resolve_gguf_path

        fallback = tmp_path / "aux" / "m.gguf"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"x")
        target_dir = tmp_path / "weight"
        result = resolve_gguf_path(
            target_dir / "m.gguf", "repo/x", "m.gguf", target_dir, fallback_path=fallback
        )
        assert result == str(target_dir / "m.gguf")
        assert (target_dir / "m.gguf").exists()

    def test_hf_download_called_when_missing(self, tmp_path, monkeypatch):
        import huggingface_hub
        from models.utils import resolve_gguf_path

        calls = {}

        def _fake_dl(**kwargs):
            calls.update(kwargs)

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", _fake_dl)
        result = resolve_gguf_path(tmp_path / "m.gguf", "repo/x", "m.gguf", tmp_path)
        assert calls["repo_id"] == "repo/x"
        assert calls["filename"] == "m.gguf"
        assert result == str(tmp_path / "m.gguf")

    def test_missing_huggingface_hub_raises_import_error(self, tmp_path, monkeypatch):
        import builtins
        from models.utils import resolve_gguf_path

        real_import = builtins.__import__

        def _blocked(name, *a, **kw):
            if name == "huggingface_hub":
                raise ImportError("blocked")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        with pytest.raises(ImportError, match="huggingface_hub"):
            resolve_gguf_path(tmp_path / "m.gguf", "repo/x", "m.gguf", tmp_path)
