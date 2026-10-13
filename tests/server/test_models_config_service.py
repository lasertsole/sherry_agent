"""Unit tests for ``server/service/models_config.py`` — the ``.models`` file.

The model configuration left ``.env`` for its own gitignored JSON5 file; the
service answers the same grouped shape GET /env uses (so the 模型配置 tab renders
it unchanged) and writes line-wise, preserving comments and the 128K floor.
"""

import json5
import pytest

from server.service import models_config as mc

SAMPLE_MODELS = """// 模型配置模板
{
  // main family
  "MAIN_LLM_PROVIDER": "openai",
  "MAIN_LLM_MAX_TOKEN": 131072,
  "MAIN_LLM_ENABLE_THINKING": false,
  "EMBEDDING_API_NAME": "bge-m3"
}
"""


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def models_file(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text(SAMPLE_MODELS, encoding="utf-8")
    monkeypatch.setattr(mc, "MODELS_CONFIG_PATH", path)
    return path


class TestReadModelsConfig:
    def test_groups_follow_the_canonical_order_and_kinds(self, models_file):
        payload = mc.read_models_config()

        assert [g["name"] for g in payload["groups"]] == ["MAIN_LLM", "EMBEDDING"]
        assert [g["kind"] for g in payload["groups"]] == ["model", "model"]
        main = payload["groups"][0]
        assert {e["key"] for e in main["entries"]} == {
            "MAIN_LLM_PROVIDER",
            "MAIN_LLM_MAX_TOKEN",
            "MAIN_LLM_ENABLE_THINKING",
        }

    def test_values_render_as_ui_strings(self, models_file):
        entries = {
            e["key"]: e["value"] for g in mc.read_models_config()["groups"] for e in g["entries"]
        }

        assert entries["MAIN_LLM_PROVIDER"] == "openai"
        assert entries["MAIN_LLM_MAX_TOKEN"] == "131072"
        assert entries["MAIN_LLM_ENABLE_THINKING"] == "false"

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mc, "MODELS_CONFIG_PATH", tmp_path / "missing.models")

        with pytest.raises(FileNotFoundError):
            mc.read_models_config()

    def test_unknown_keys_are_not_surfaced(self, tmp_path, monkeypatch):
        path = tmp_path / ".models"
        path.write_text(
            '{\n  "NOT_A_MODEL_KEY": "x",\n  "TTI_API_NAME": "tti"\n}\n', encoding="utf-8"
        )
        monkeypatch.setattr(mc, "MODELS_CONFIG_PATH", path)

        keys = {e["key"] for g in mc.read_models_config()["groups"] for e in g["entries"]}
        assert keys == {"TTI_API_NAME"}


class TestWriteModelsConfig:
    def test_writes_typed_values_and_keeps_comments(self, models_file):
        mc.write_models_config({"MAIN_LLM_ENABLE_THINKING": "true", "MAIN_LLM_MAX_TOKEN": "262144"})

        text = models_file.read_text(encoding="utf-8")
        assert "// 模型配置模板" in text
        assert "// main family" in text
        assert '"MAIN_LLM_ENABLE_THINKING": true' in text
        assert '"MAIN_LLM_MAX_TOKEN": 262144' in text
        # The old numeric value is gone (line was replaced, not duplicated).
        assert text.count('"MAIN_LLM_MAX_TOKEN"') == 1

    def test_keeps_a_bak_of_the_pre_write_state(self, models_file):
        mc.write_models_config({"MAIN_LLM_PROVIDER": "anthropic"})

        assert (models_file.parent / ".models.bak").read_text(encoding="utf-8") == SAMPLE_MODELS

    def test_rejects_unknown_keys(self, models_file):
        with pytest.raises(ValueError, match="Unknown models config keys"):
            mc.write_models_config({"NOPE": "1"})

    def test_enforces_the_128k_floor(self, models_file):
        with pytest.raises(ValueError, match="128K"):
            mc.write_models_config({"MAIN_LLM_MAX_TOKEN": "65536"})

    def test_a_rejected_write_leaves_the_file_untouched(self, models_file):
        """A sub-128K value must never reach disk — and no .bak churn either."""
        with pytest.raises(ValueError, match="128K"):
            mc.write_models_config({"MAIN_LLM_MAX_TOKEN": "65536"})

        assert models_file.read_text(encoding="utf-8") == SAMPLE_MODELS
        assert not (models_file.parent / ".models.bak").exists()

    def test_rejects_a_non_integer_max_token(self, models_file):
        with pytest.raises(ValueError, match="must be an integer"):
            mc.write_models_config({"AUXILIARY_LLM_MAX_TOKEN": "lots"})

    def test_appends_a_known_key_that_has_no_line(self, models_file):
        mc.write_models_config({"STT_API_NAME": "whisper-large"})

        data = json5.loads(models_file.read_text(encoding="utf-8"))
        assert data["STT_API_NAME"] == "whisper-large"
        # The append did not disturb the existing lines.
        assert data["MAIN_LLM_PROVIDER"] == "openai"
