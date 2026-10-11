"""Unit tests for ``server/service/env.py`` — split-out keys exclusion.

Three key families have LEFT ``.env`` and must never surface through GET /env
nor be writable through PUT /env, even if a legacy .env still carries them:

* application settings → ``sherry.jsonc`` (``config/sherry_settings.py``),
* the model configuration → ``.models`` (``config/models_settings.py``),
* the network services → ``.network`` (``config/network_settings.py``).

What remains in ``.env`` is the non-secret application configuration (sandbox /
gateway / login) — and that file is tracked in git.
"""

import pytest

from server.service.env import SPLIT_OUT_KEYS, read_env_file, write_env_file

SAMPLE_ENV = """SANDBOX_POLICY = required

# Legacy model keys that moved to .models:
MAIN_LLM_PROVIDER = "openai"
MAIN_LLM_API_KEY = "sk-live"

# Legacy search-backend keys that moved to .network (owned by the 模型联网 panel):
TAVILY_API_KEY = "tvly-secret"
SEARXNG_URL = "http://localhost:8888"

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
        # Moved families are hidden too — the model keys went to .models and the
        # search keys to .network.
        assert "MAIN_LLM_API_KEY" not in all_keys
        assert "TAVILY_API_KEY" not in all_keys
        # A key that still belongs to .env stays visible.
        assert "SANDBOX_POLICY" in all_keys

    def test_write_rejects_sherry_keys(self, env_file):
        with pytest.raises(ValueError, match="moved to sherry.jsonc"):
            write_env_file({"LOG_LEVEL": "TRACE"})

    def test_write_rejects_model_keys(self, env_file):
        with pytest.raises(ValueError, match="moved to .models"):
            write_env_file({"MAIN_LLM_API_KEY": "sk-rotated"})

    def test_write_rejects_network_keys(self, env_file):
        with pytest.raises(ValueError, match="moved to .network"):
            write_env_file({"TAVILY_API_KEY": "tvly-new", "SEARXNG_URL": "http://x"})

    def test_write_still_accepts_regular_keys(self, env_file):
        write_env_file({"SANDBOX_POLICY": "auto"})
        assert "auto" in env_file.read_text(encoding="utf-8")

    def test_write_no_longer_keeps_a_bak_next_to_a_tracked_env(self, env_file):
        write_env_file({"SANDBOX_POLICY": "auto"})

        # .env is TRACKED now (git is the backup) — the service stops leaving a
        # .env.bak behind on every save.
        assert not (env_file.parent / ".env.bak").exists()


class TestPanelOwnedKeys:
    """A DEDICATED panel owns the search keys (the 模型联网 tab).

    They live in ``.network`` now: withheld from GET /env, rejected by PUT /env,
    written through PUT /network-config, and read back through
    ``network_file_values()``.
    """

    def test_hidden_keys_never_surface_in_the_read(self, env_file):
        payload = read_env_file()
        keys = {e["key"] for g in payload["groups"] for e in g["entries"]}

        assert "TAVILY_API_KEY" not in keys
        assert not any(key.startswith("SEARXNG_") for key in keys)

    def test_hidden_keys_are_no_longer_writable_through_env(self, env_file):
        with pytest.raises(ValueError, match="moved to .network"):
            write_env_file({"TAVILY_API_KEY": "tvly-new"})

    def test_env_file_values_still_returns_the_raw_file(self, env_file):
        from server.service.env import env_file_values

        values = env_file_values()

        # The raw file view is unfiltered on purpose (it is what a save would
        # rewrite); the panels select the keys they own.
        assert values["TAVILY_API_KEY"] == "tvly-secret"
        assert values["SEARXNG_URL"] == "http://localhost:8888"
        assert values["MAIN_LLM_PROVIDER"] == "openai"

    def test_env_file_values_yields_empty_without_a_file(self, tmp_path, monkeypatch):
        from server.service.env import env_file_values

        monkeypatch.setattr("server.service.env.ENV_PATH", tmp_path / "missing.env")

        assert env_file_values() == {}


class TestGroupOrder:
    """GET /env group order is the env-config UI's display order.

    The order comes from ``SERVER_HTTP["env_group_prefixes"]`` (a tuple, not the
    .env block order). The model families no longer live in .env, but the tuple
    still drives the 模型配置 tab's merge (the .models / .network payloads use
    the same table), so the canonical positions stay pinned here.
    """

    GROUPED_ENV = """SANDBOX_POLICY = required
SOME_OTHER_KEY = "x"

# Legacy model key — hidden (moved to .models).
MAIN_LLM_PROVIDER = "openai"
"""

    def test_canonical_prefix_order(self):
        from config.features import SERVER_HTTP

        assert SERVER_HTTP["env_group_prefixes"][:3] == (
            "MAIN_LLM_",
            "AUXILIARY_LLM_",
            "REASONER_LLM_",
        )
        # The network file's groups render last, after the model families.
        assert SERVER_HTTP["env_group_prefixes"][-2:] == ("SHERRY_BROWSER_", "LANGSMITH_")

    def test_read_env_file_hides_the_moved_families(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        path.write_text(self.GROUPED_ENV, encoding="utf-8")
        monkeypatch.setattr("server.service.env.ENV_PATH", path)

        names = [group["name"] for group in read_env_file()["groups"]]
        # Only the keys that still belong to .env are grouped; the legacy model
        # line is hidden, so the payload is the catch-all group alone.
        assert names == ["other"]
        keys = {e["key"] for g in read_env_file()["groups"] for e in g["entries"]}
        assert keys == {"SANDBOX_POLICY", "SOME_OTHER_KEY"}
