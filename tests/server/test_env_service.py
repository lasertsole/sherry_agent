"""Unit tests for ``server/service/env.py`` — split-out keys exclusion.

The app-level keys that moved to ``sherry.jsonc`` (see
``config/sherry_settings.py``) must never surface through GET /env nor be
writable through PUT /env, even if they are still present in a legacy .env.
"""

import pytest

from server.service.env import SPLIT_OUT_KEYS, read_env_file, write_env_file

SAMPLE_ENV = """MAIN_LLM_PROVIDER = "openai"
MAIN_LLM_API_KEY = "sk-live"

# TAVILY_API_KEY is a regular .env key again (secret-class values live here,
# NOT in the tracked sherry.jsonc):
TAVILY_API_KEY = "tvly-secret"

# Legacy app-level keys that moved to sherry.jsonc:
TOOL_CALL_TIMEOUT_MINUTES = 5
LOG_LEVEL = "DEBUG"
"""


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(SAMPLE_ENV, encoding="utf-8")
    monkeypatch.setattr("server.service.env.ENV_PATH", path)
    return path


class TestSplitOutKeysExclusion:
    def test_split_out_keys_never_surface_in_read(self, env_file):
        payload = read_env_file()
        all_keys = {e["key"] for g in payload["groups"] for e in g["entries"]}
        assert all_keys.isdisjoint(SPLIT_OUT_KEYS)
        assert "MAIN_LLM_API_KEY" in all_keys
        # TAVILY_API_KEY is back as a regular (secret-class) .env key.
        assert "TAVILY_API_KEY" in all_keys

    def test_write_rejects_split_out_keys(self, env_file):
        with pytest.raises(ValueError, match="moved to sherry.jsonc"):
            write_env_file({"LOG_LEVEL": "TRACE"})

    def test_write_still_accepts_regular_keys(self, env_file):
        write_env_file({"MAIN_LLM_API_KEY": "sk-rotated"})
        assert "sk-rotated" in env_file.read_text(encoding="utf-8")
