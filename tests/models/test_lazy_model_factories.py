"""Lazy construction for the ITTT/VTTT/extract/reranker singletons.

Importing a model module must not build a client or resolve/download weights;
``build_*()`` factories are the unified entry point and the module-level
singletons stay usable through a lazy proxy (same instance per process).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import config

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_ROOT = Path(__file__).parents[2]

_ITTT_REMOTE_ENV = {
    "ITTT_MODEL_LOCAL": "false",
    "ITTT_MODEL_PROVIDER": "openai",
    "ITTT_API_NAME": "gpt-4o-mini",
    "ITTT_API_KEY": "sk-test",
    "ITTT_API_BASE": "https://example.invalid/v1",
}

_VTTT_REMOTE_ENV = {
    "VTTT_MODEL_LOCAL": "false",
    "VTTT_MODEL_PROVIDER": "openai",
    "VTTT_API_NAME": "gpt-4o-mini",
    "VTTT_API_KEY": "sk-test",
    "VTTT_API_BASE": "https://example.invalid/v1",
}


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConfigurable:
    """Stand-in for the ``configurable_fields(...)`` runnable."""

    def __init__(self) -> None:
        self.default = self

    def configurable_fields(self, **_kwargs):
        return self


class _CountingInitChatModel:
    """Counts constructions; import must leave the count at zero."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_kwargs) -> _FakeConfigurable:
        self.calls += 1
        return _FakeConfigurable()


def _remote_load(monkeypatch, tmp_path, module_name, relpath, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "missing.env"))
    stub = _CountingInitChatModel()
    import langchain.chat_models as langchain_chat_models

    monkeypatch.setattr(langchain_chat_models, "init_chat_model", stub)
    module = _load(module_name, relpath)
    return module, stub


class TestRemoteLaziness:
    def test_ittt_remote_import_does_not_build(self, monkeypatch, tmp_path):
        module, stub = _remote_load(
            monkeypatch,
            tmp_path,
            "_ittt_lazy_remote",
            "models/ITTT_model/core.py",
            _ITTT_REMOTE_ENV,
        )

        assert stub.calls == 0
        first = module.ITTT_model.default
        assert stub.calls == 1
        assert module.ITTT_model.default is first
        assert stub.calls == 1

    def test_vttt_remote_import_does_not_build(self, monkeypatch, tmp_path):
        module, stub = _remote_load(
            monkeypatch,
            tmp_path,
            "_vttt_lazy_remote",
            "models/VTTT_model/core.py",
            _VTTT_REMOTE_ENV,
        )

        assert stub.calls == 0
        first = module.VTTT_model.default
        assert stub.calls == 1
        assert module.VTTT_model.default is first
        assert stub.calls == 1


class TestLocalLaziness:
    def test_ittt_local_import_does_not_resolve_weights(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ITTT_MODEL_LOCAL", "true")
        monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "missing.env"))
        import models.utils as models_utils

        calls = {"n": 0}

        def _fake_resolve(*_args, **_kwargs):
            calls["n"] += 1
            return "/tmp/fake-model.gguf"

        monkeypatch.setattr(models_utils, "resolve_gguf_path", _fake_resolve)
        module = _load("_ittt_lazy_local", "models/ITTT_model/core.py")

        assert calls["n"] == 0  # import resolves nothing
        module.ITTT_model.invoke
        after_first = calls["n"]
        assert after_first >= 1  # first access resolves gguf + mmproj
        module.ITTT_model.invoke
        assert calls["n"] == after_first  # singleton — no re-resolution


class TestFactories:
    def test_ittt_factory_builds_fresh_instances(self, monkeypatch, tmp_path):
        module, stub = _remote_load(
            monkeypatch,
            tmp_path,
            "_ittt_lazy_factory",
            "models/ITTT_model/core.py",
            _ITTT_REMOTE_ENV,
        )

        assert stub.calls == 0
        module.build_ittt_model()
        module.build_ittt_model()
        assert stub.calls == 2  # factory, like build_main_llm, is not memoized

    def test_build_mineru_model_returns_the_singleton(self):
        from models.extract_model import build_mineru_model, mineru_model
        from models.extract_model.core import MinerUModel

        assert build_mineru_model() is mineru_model
        assert build_mineru_model() is MinerUModel.get_instance()

    def test_build_reranker_model_returns_lazy_reranker(self):
        from models.reranker_model import _LazyReranker, build_reranker_model

        instance = build_reranker_model()
        assert isinstance(instance, _LazyReranker)
        for method in ("rank", "filter", "predict", "predict_scores"):
            assert callable(getattr(instance, method))

    def test_models_package_exposes_every_factory(self):
        import models

        for name in (
            "build_ittt_model",
            "build_vttt_model",
            "build_mineru_model",
            "build_reranker_model",
        ):
            assert callable(getattr(models, name))
