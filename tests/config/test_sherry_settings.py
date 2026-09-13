"""Unit tests for ``config/sherry_settings.py`` — the sherry.jsonc settings loader."""

import json

import json5
import pytest

from config import sherry_settings


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def sherry_file(tmp_path, monkeypatch):
    """Point SHERRY_CONFIG_PATH at a temp file for the duration of a test."""
    path = tmp_path / "sherry.jsonc"
    monkeypatch.setattr(sherry_settings, "SHERRY_CONFIG_PATH", path)
    return path


class TestLoadSherrySettings:
    def test_missing_file_yields_typed_defaults(self, sherry_file):
        settings = sherry_settings.load_sherry_settings()
        assert settings == dict(sherry_settings.SHERRY_SETTING_DEFAULTS)
        assert settings["TOOL_CALL_TIMEOUT_MINUTES"] == 5
        assert settings["LANGSMITH.TRACING_V2"] is False
        assert settings["curator.interval_hours"] == 168
        # Secret-class keys are not part of sherry.jsonc at all.
        assert "TAVILY_API_KEY" not in settings

    def test_comments_and_trailing_commas_parse(self, sherry_file):
        sherry_file.write_text(
            "// app settings\n"
            "{\n"
            "  // timeout in minutes\n"
            '  "TOOL_CALL_TIMEOUT_MINUTES": 15,\n'
            '  "LOG_LEVEL": "DEBUG",\n'
            "}",
            encoding="utf-8",
        )
        settings = sherry_settings.load_sherry_settings()
        assert settings["TOOL_CALL_TIMEOUT_MINUTES"] == 15
        assert settings["LOG_LEVEL"] == "DEBUG"
        # Untouched keys fall back to their typed defaults.
        assert settings["LANGSMITH.TRACING_V2"] is False
        assert settings["curator.enabled"] is True

    def test_group_values_are_coerced_to_declared_types(self, sherry_file):
        sherry_file.write_text(
            json.dumps(
                {
                    "LANGSMITH": {"TRACING_V2": "true"},
                    "curator": {
                        "interval_hours": "24",
                        "min_idle_hours": "0.5",
                        "consolidate": "false",
                    },
                }
            ),
            encoding="utf-8",
        )
        settings = sherry_settings.load_sherry_settings()
        assert settings["LANGSMITH.TRACING_V2"] is True
        assert settings["curator.interval_hours"] == 24
        assert isinstance(settings["curator.interval_hours"], int)
        assert settings["curator.min_idle_hours"] == 0.5
        assert settings["curator.consolidate"] is False

    def test_non_dict_group_falls_back_to_defaults(self, sherry_file):
        sherry_file.write_text(
            json.dumps({"LANGSMITH": "oops", "curator": {"interval_hours": 24}}),
            encoding="utf-8",
        )
        settings = sherry_settings.load_sherry_settings()
        assert settings["LANGSMITH.TRACING_V2"] is False
        assert settings["LANGSMITH.PROJECT"] == "EMA_AI_agent"
        assert settings["curator.interval_hours"] == 24

    def test_unparseable_file_yields_defaults(self, sherry_file):
        sherry_file.write_text("{ not json at all", encoding="utf-8")
        assert sherry_settings.load_sherry_settings() == dict(
            sherry_settings.SHERRY_SETTING_DEFAULTS
        )


class TestGetSherrySetting:
    def test_unknown_key_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown sherry setting"):
            sherry_settings.get_sherry_setting("NOT_A_KEY")

    def test_parse_sherry_value_coerces_ui_strings(self):
        assert sherry_settings.parse_sherry_value("TOOL_CALL_TIMEOUT_MINUTES", "7") == 7
        assert sherry_settings.parse_sherry_value("LANGSMITH.TRACING_V2", "false") is False
        assert sherry_settings.parse_sherry_value("LANGSMITH.TRACING_V2", "true") is True
        assert sherry_settings.parse_sherry_value("curator.min_idle_hours", "2.5") == 2.5
        assert sherry_settings.parse_sherry_value("LOG_LEVEL", "DEBUG") == "DEBUG"

    def test_parse_sherry_value_rejects_bad_int(self):
        with pytest.raises(ValueError):
            sherry_settings.parse_sherry_value("TOOL_CALL_TIMEOUT_MINUTES", "abc")

    def test_parse_sherry_value_rejects_unknown_key(self):
        with pytest.raises(KeyError):
            sherry_settings.parse_sherry_value("NOT_A_KEY", "x")


class TestShippedConfigFile:
    def test_repo_sherry_jsonc_is_valid_and_complete(self):
        """The shipped sherry.jsonc must parse and carry every known key."""

        def dotted_keys(node, prefix=""):
            for name, value in node.items():
                path = f"{prefix}{name}"
                if isinstance(value, dict):
                    yield from dotted_keys(value, f"{path}.")
                else:
                    yield path

        data = json5.loads(sherry_settings.SHERRY_CONFIG_PATH.read_text(encoding="utf-8"))
        assert list(dotted_keys(data)) == list(sherry_settings.SHERRY_SETTING_KEYS)
