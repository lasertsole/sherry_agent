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


class TestGroupOrder:
    """GET /env group order is the env-config UI's display order.

    The order comes from ``SERVER_HTTP["env_group_prefixes"]`` (a tuple, not the
    .env block order), so the chat model, the auxiliary model and the reasoner
    must stay in the positions the project documents — a reorder here silently
    reorders the configuration dialog.
    """

    GROUPED_ENV = """MAIN_LLM_PROVIDER = "openai"
REASONER_LLM_PROVIDER = "openai"
AUXILIARY_LLM_PROVIDER = "openai"
ITTT_API_NAME = "glm"
VTTT_API_NAME = "glm"
TTI_API_NAME = "tti"
RERANKER_API_NAME = "rerank"
EMBEDDING_API_NAME = "embed"
STT_API_NAME = "stt"
SOME_OTHER_KEY = "x"
"""

    def test_canonical_prefix_order(self):
        from config.features import SERVER_HTTP

        assert SERVER_HTTP["env_group_prefixes"][:3] == (
            "MAIN_LLM_",
            "AUXILIARY_LLM_",
            "REASONER_LLM_",
        )

    def test_read_env_file_follows_the_canonical_order(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        # Deliberately written in a DIFFERENT order than the canonical one: the
        # response must still follow the configured group order.
        path.write_text(self.GROUPED_ENV, encoding="utf-8")
        monkeypatch.setattr("server.service.env.ENV_PATH", path)

        names = [group["name"] for group in read_env_file()["groups"]]
        assert names[:3] == ["MAIN_LLM", "AUXILIARY_LLM", "REASONER_LLM"]
        # Unknown keys still collect in the trailing catch-all group.
        assert names[-1] == "other"
