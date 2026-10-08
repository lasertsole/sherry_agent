"""Tests for the process-level LLM multimodal capability cache."""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent.middlewares import llm_capability_cache as cache

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.reset_cache()
    yield
    cache.reset_cache()


class TestReadWrite:
    def test_unknown_pair_returns_auto(self):
        assert cache.get_capability("openai", "gpt-4o", "vision") == "auto"

    @pytest.mark.parametrize("media", ["vision", "audio", "video"])
    def test_roundtrip_per_media_type(self, media):
        cache.set_capability("openai", "gpt-4o", media, "supported")
        assert cache.get_capability("openai", "gpt-4o", media) == "supported"

        cache.set_capability("openai", "gpt-4o", media, "unsupported")
        assert cache.get_capability("openai", "gpt-4o", media) == "unsupported"

    def test_media_types_are_independent(self):
        cache.set_capability("openai", "gpt-4o", "vision", "supported")
        cache.set_capability("openai", "gpt-4o", "video", "unsupported")

        assert cache.get_capability("openai", "gpt-4o", "vision") == "supported"
        assert cache.get_capability("openai", "gpt-4o", "video") == "unsupported"
        assert cache.get_capability("openai", "gpt-4o", "audio") == "auto"


class TestModelKeyIsolation:
    def test_same_model_name_under_different_providers_is_isolated(self):
        cache.set_capability("deepseek", "shared-name", "vision", "unsupported")

        assert cache.get_capability("deepseek", "shared-name", "vision") == "unsupported"
        assert cache.get_capability("openai", "shared-name", "vision") == "auto"

    def test_same_provider_with_different_models_is_isolated(self):
        cache.set_capability("openai", "gpt-4o", "vision", "supported")

        assert cache.get_capability("openai", "gpt-4o", "vision") == "supported"
        assert cache.get_capability("openai", "gpt-4o-mini", "vision") == "auto"


class TestResetCache:
    def test_reset_clears_every_model_and_media(self):
        cache.set_capability("openai", "gpt-4o", "vision", "supported")
        cache.set_capability("deepseek", "deepseek-chat", "video", "unsupported")

        cache.reset_cache()

        assert cache.get_capability("openai", "gpt-4o", "vision") == "auto"
        assert cache.get_capability("deepseek", "deepseek-chat", "video") == "auto"


class TestGetModelKey:
    def test_reads_provider_and_model_from_env(self, monkeypatch):
        monkeypatch.setenv("MAIN_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MAIN_LLM_NAME", "gpt-4o")

        assert cache.get_model_key() == "openai/gpt-4o"

    def test_missing_env_yields_empty_segments(self, monkeypatch):
        monkeypatch.delenv("MAIN_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("MAIN_LLM_NAME", raising=False)

        assert cache.get_model_key() == "/"


class TestConcurrency:
    def test_concurrent_writes_from_many_threads_are_all_recorded(self):
        errors: list[Exception] = []

        def writer(worker: int) -> None:
            try:
                for index in range(100):
                    cache.set_capability(
                        f"provider-{worker}", f"model-{index}", "vision", "supported"
                    )
                    cache.get_capability(f"provider-{worker}", f"model-{index}", "vision")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        for worker in range(8):
            assert cache.get_capability(f"provider-{worker}", "model-99", "vision") == "supported"

    def test_concurrent_same_key_writes_stay_consistent(self):
        values = ("auto", "supported", "unsupported")

        def writer(value: str) -> None:
            for _ in range(200):
                cache.set_capability("openai", "gpt-4o", "vision", value)

        threads = [threading.Thread(target=writer, args=(value,)) for value in values]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert all(not thread.is_alive() for thread in threads)
        assert cache.get_capability("openai", "gpt-4o", "vision") in values


class TestProcessScope:
    def test_state_is_shared_within_the_process(self):
        cache.set_capability("openai", "gpt-4o", "vision", "supported")

        assert cache.get_capability("openai", "gpt-4o", "vision") == "supported"
        cache.reset_cache()
        assert cache.get_capability("openai", "gpt-4o", "vision") == "auto"

    def test_state_does_not_leak_across_processes(self):
        cache.set_capability("openai", "gpt-4o", "vision", "supported")
        module_path = Path(cache.__file__).resolve()
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('probe_cache', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.get_capability('openai', 'gpt-4o', 'vision'))\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", code, str(module_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "auto"
