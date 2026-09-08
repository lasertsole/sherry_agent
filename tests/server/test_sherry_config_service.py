"""Unit tests for ``server/service/sherry_config.py`` — sherry.jsonc read/write service."""

import json5
import pytest

from config import sherry_settings
from server.service.sherry_config import read_sherry_config, write_sherry_config

SAMPLE = """// app settings
{
  // timeout in minutes
  "TOOL_CALL_TIMEOUT_MINUTES": 5,

  "LOG_LEVEL": "INFO",

  "SUBAGENT_TODO_DONE_FUNC": "archive",
  "WORKSPACE_TEMPLATE_LANG": "en",
  "LANGSMITH_TRACING_V2": false,
  "LANGSMITH_API_KEY": "",
  "LANGSMITH_PROJECT": "EMA_AI_agent"
}
"""


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def sherry_file(tmp_path, monkeypatch):
    """A temp sherry.jsonc with the shipped shape; the service path is patched to it."""
    path = tmp_path / "sherry.jsonc"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr("server.service.sherry_config.SHERRY_CONFIG_PATH", path)
    return path


class TestReadSherryConfig:
    def test_reads_every_entry_in_file_order_as_ui_strings(self, sherry_file):
        payload = read_sherry_config()
        keys = [e["key"] for e in payload["entries"]]
        assert keys == list(sherry_settings.SHERRY_SETTING_KEYS)

        by_key = {e["key"]: e["value"] for e in payload["entries"]}
        assert by_key["TOOL_CALL_TIMEOUT_MINUTES"] == "5"
        assert by_key["LANGSMITH_TRACING_V2"] == "false"
        assert by_key["LOG_LEVEL"] == "INFO"
        assert all(e["value_edited"] is False for e in payload["entries"])

    def test_missing_file_raises_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "server.service.sherry_config.SHERRY_CONFIG_PATH", tmp_path / "nope.jsonc"
        )
        with pytest.raises(FileNotFoundError):
            read_sherry_config()


class TestWriteSherryConfig:
    def test_write_coerces_types_and_preserves_comments_and_order(self, sherry_file):
        write_sherry_config(
            {
                "LOG_LEVEL": "DEBUG",
                "TOOL_CALL_TIMEOUT_MINUTES": "9",
                "LANGSMITH_TRACING_V2": "true",
            }
        )
        text = sherry_file.read_text(encoding="utf-8")

        # Comments and ordering survive the line-wise edit.
        assert "// app settings" in text
        assert "// timeout in minutes" in text
        keys_in_order = [
            line.split('"')[1] for line in text.splitlines() if line.strip().startswith('"')
        ]
        assert keys_in_order == list(sherry_settings.SHERRY_SETTING_KEYS)

        # Typed values are written as JSON scalars (no quotes on int/bool).
        assert '"TOOL_CALL_TIMEOUT_MINUTES": 9,' in text
        assert '"LOG_LEVEL": "DEBUG",' in text
        assert '"LANGSMITH_TRACING_V2": true' in text

        # The trailing comma of the last entry stays absent; the file still parses.
        assert not text.rstrip().rstrip(",").endswith(",")
        data = json5.loads(text)
        assert data["TOOL_CALL_TIMEOUT_MINUTES"] == 9
        assert data["LANGSMITH_TRACING_V2"] is True
        assert data["LANGSMITH_PROJECT"] == "EMA_AI_agent"

    def test_write_creates_a_dot_bak_backup(self, sherry_file):
        write_sherry_config({"LOG_LEVEL": "DEBUG"})
        backup = sherry_file.parent / "sherry.jsonc.bak"
        assert backup.exists()
        assert '"LOG_LEVEL": "INFO"' in backup.read_text(encoding="utf-8")

    def test_write_rejects_unknown_keys(self, sherry_file):
        with pytest.raises(ValueError, match="Unknown sherry config keys"):
            write_sherry_config({"NOT_A_KEY": "x"})

    def test_write_rejects_bad_int_and_leaves_file_untouched(self, sherry_file):
        with pytest.raises(ValueError):
            write_sherry_config({"TOOL_CALL_TIMEOUT_MINUTES": "abc"})
        # Pre-write validation: the file (and backup) must not have been touched.
        assert '"TOOL_CALL_TIMEOUT_MINUTES": 5' in sherry_file.read_text(encoding="utf-8")
        assert not (sherry_file.parent / "sherry.jsonc.bak").exists()

    def test_write_rejects_non_mapping(self, sherry_file):
        with pytest.raises(ValueError, match="mapping"):
            write_sherry_config(["not", "a", "mapping"])  # type: ignore[arg-type]
