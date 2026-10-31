"""Unit tests for ``server/service/sherry_config.py`` — sherry.jsonc read/write service."""

import json5
import pytest

from config import sherry_settings
from server.service.sherry_config import read_sherry_config, write_sherry_config

SAMPLE = """// app settings
{
  // timeout in minutes
  "TOOL_CALL_TIMEOUT_MINUTES": 5,

  // default session cap (0 = unlimited)
  "TOKEN_BUDGET_DEFAULT_CAP": 0,

  // default per-turn cap (0 = unlimited)
  "TOKEN_BUDGET_DEFAULT_TURN_CAP": 0,

  "LOG_LEVEL": "INFO",

  "project_dir": "",

  "WORKSPACE_TEMPLATE_LANG": "en",
  "safety_filter": {
    "enabled": false,
    "llm_moderation_mode": "off",
    "llm_moderation_model": "",
    "keyword_file": "sensitive_words.json"
  },
  "heartbeat": {
    "enabled": true
  },
  "curator": {
    "enabled": true,
    "interval_hours": 168,
    "min_idle_hours": 2,
    "stale_after_days": 30,
    "archive_after_days": 90,
    "consolidate": true,
    "prune_builtins": true
  },
  // hooks: the master switch; the per-event hook lists are read by
  // load_sherry_hooks (lists of objects), not by this scalar service.
  "hooks": {
    "enabled": false
  }
}
"""


pytestmark = [pytest.mark.unit]


def _dotted_keys(text: str) -> list[str]:
    """Parse a sherry.jsonc document into its dotted setting keys, in file order."""
    keys: list[str] = []
    group = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("}"):
            group = None
            continue
        if not stripped.startswith('"'):
            continue
        name = stripped.split('"')[1]
        rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        if rest == "{":
            group = name
            continue
        keys.append(f"{group}.{name}" if group else name)
    return keys


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
        assert by_key["heartbeat.enabled"] == "true"
        assert by_key["curator.interval_hours"] == "168"
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
                "heartbeat.enabled": "false",
                "curator.consolidate": "false",
            }
        )
        text = sherry_file.read_text(encoding="utf-8")

        # Comments and ordering survive the line-wise edit.
        assert "// app settings" in text
        assert "// timeout in minutes" in text
        assert _dotted_keys(text) == list(sherry_settings.SHERRY_SETTING_KEYS)

        # Typed values are written as JSON scalars (no quotes on int/bool).
        assert '"TOOL_CALL_TIMEOUT_MINUTES": 9,' in text
        assert '"LOG_LEVEL": "DEBUG",' in text
        assert '"enabled": false' in text
        assert '"consolidate": false' in text

        # The trailing comma of the last entry stays absent; the file still parses.
        assert not text.rstrip().rstrip(",").endswith(",")
        data = json5.loads(text)
        assert data["TOOL_CALL_TIMEOUT_MINUTES"] == 9
        assert data["heartbeat"]["enabled"] is False
        assert data["curator"]["consolidate"] is False

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


class TestListSettings:
    """The list-settings API: only registered keys, validated entries, safe edits.

    Lists sit outside the typed scalar registry (``load_sherry_list_setting``),
    so they get their own read/write pair with a KEY ALLOWLIST — a free-form key
    would let the UI author settings nothing consumes.
    """

    @staticmethod
    def _service(monkeypatch, tmp_path, text: str):
        import server.service.sherry_config as service

        config = tmp_path / "sherry.jsonc"
        config.write_text(text, encoding="utf-8")
        monkeypatch.setattr(service, "SHERRY_CONFIG_PATH", config)
        return service, config

    def test_reading_a_missing_key_is_an_empty_list(self, monkeypatch, tmp_path):
        service, _ = self._service(monkeypatch, tmp_path, '{\n  "LOG_LEVEL": "INFO",\n}\n')

        assert service.read_sherry_list("regression_gate_allowed_executables") == []

    def test_an_unknown_key_is_refused_both_ways(self, monkeypatch, tmp_path):
        service, _ = self._service(monkeypatch, tmp_path, "{}\n")

        with pytest.raises(ValueError, match="Unknown list setting"):
            service.read_sherry_list("not_a_registered_key")
        with pytest.raises(ValueError, match="Unknown list setting"):
            service.write_sherry_list("not_a_registered_key", ["x"])

    def test_write_then_read_round_trips(self, monkeypatch, tmp_path):
        service, config = self._service(
            monkeypatch, tmp_path, '{\n  // keep me\n  "LOG_LEVEL": "INFO",\n}\n'
        )

        written = service.write_sherry_list(
            "regression_gate_allowed_executables", ["Gradle", "  maven  "]
        )

        assert written == ["gradle", "maven"], "entries are normalized"
        assert service.read_sherry_list("regression_gate_allowed_executables") == [
            "gradle",
            "maven",
        ]
        text = config.read_text(encoding="utf-8")
        assert "// keep me" in text, "comments survive the line-wise edit"
        assert '"LOG_LEVEL": "INFO",' in text

    def test_an_existing_value_is_replaced_in_place(self, monkeypatch, tmp_path):
        service, config = self._service(
            monkeypatch,
            tmp_path,
            '{\n  "regression_gate_allowed_executables": ["rspec"],\n  "LOG_LEVEL": "INFO",\n}\n',
        )

        service.write_sherry_list("regression_gate_allowed_executables", ["dotnet"])

        lines = [
            line for line in config.read_text(encoding="utf-8").splitlines() if "regression" in line
        ]
        assert lines == ['  "regression_gate_allowed_executables": ["dotnet"],']
        assert '"LOG_LEVEL": "INFO",' in config.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "values",
        [
            ["gradle; rm -rf /"],
            ["gradle && whoami"],
            ["x" * 300],
            ["ok", 3],
        ],
    )
    def test_bad_entries_are_refused_and_the_file_is_untouched(self, monkeypatch, tmp_path, values):
        original = '{\n  "LOG_LEVEL": "INFO",\n}\n'
        service, config = self._service(monkeypatch, tmp_path, original)

        with pytest.raises(ValueError):
            service.write_sherry_list("regression_gate_allowed_executables", values)

        assert config.read_text(encoding="utf-8") == original

    def test_a_multi_line_array_is_refused_rather_than_corrupted(self, monkeypatch, tmp_path):
        original = '{\n  "regression_gate_allowed_executables": [\n    "gradle"\n  ],\n}\n'
        service, config = self._service(monkeypatch, tmp_path, original)

        with pytest.raises(ValueError, match="single line"):
            service.write_sherry_list("regression_gate_allowed_executables", ["dotnet"])

        assert config.read_text(encoding="utf-8") == original

    def test_the_yolo_deny_list_uses_the_same_endpoint(self, monkeypatch, tmp_path):
        service, _ = self._service(monkeypatch, tmp_path, '{\n  "LOG_LEVEL": "INFO",\n}\n')

        service.write_sherry_list("yolo_deny_paths", ["~/.docker/"])

        assert service.read_sherry_list("yolo_deny_paths") == ["~/.docker/"]

    def test_a_missing_key_is_inserted_inside_the_object(self, monkeypatch, tmp_path):
        """Appending past the closing brace would make the document unparseable."""
        service, config = self._service(
            monkeypatch, tmp_path, '{\n  // note\n  "LOG_LEVEL": "INFO"\n}\n'
        )

        service.write_sherry_list("regression_gate_allowed_executables", ["gradle"])

        text = config.read_text(encoding="utf-8")
        assert json5.loads(text) == {
            "LOG_LEVEL": "INFO",
            "regression_gate_allowed_executables": ["gradle"],
        }
        assert "// note" in text
        assert text.index("regression_gate") < text.index("}"), "inside the object"

    def test_a_single_line_document_is_refused_with_a_reason(self, monkeypatch, tmp_path):
        service, _ = self._service(monkeypatch, tmp_path, "{}\n")

        with pytest.raises(ValueError, match="multi-line top-level object"):
            service.write_sherry_list("yolo_deny_paths", ["~/.docker/"])


class TestDefaultsAndMissingKeys:
    def test_every_entry_carries_its_registry_default(self, sherry_file):
        payload = read_sherry_config()
        by_key = {e["key"]: e for e in payload["entries"]}

        assert by_key["TOOL_CALL_TIMEOUT_MINUTES"]["default"] == "5"
        assert by_key["LOG_LEVEL"]["default"] == "INFO"
        assert by_key["heartbeat.enabled"]["default"] == "false"
        assert by_key["TOKEN_BUDGET_DEFAULT_CAP"]["default"] == "0"

    def test_a_registered_key_missing_from_the_file_is_still_listed(self, tmp_path, monkeypatch):
        path = tmp_path / "sherry.jsonc"
        # An older file: no TOKEN_BUDGET_DEFAULT_CAP line at all.
        path.write_text('{\n  "LOG_LEVEL": "DEBUG"\n}\n', encoding="utf-8")
        monkeypatch.setattr("server.service.sherry_config.SHERRY_CONFIG_PATH", path)

        payload = read_sherry_config()
        by_key = {e["key"]: e for e in payload["entries"]}

        assert by_key["LOG_LEVEL"]["value"] == "DEBUG"
        cap = by_key["TOKEN_BUDGET_DEFAULT_CAP"]
        assert cap["value"] == "0" and cap["default"] == "0"
        # File order first, then the missing registry keys.
        assert [e["key"] for e in payload["entries"]][0] == "LOG_LEVEL"

    def test_writing_a_missing_key_appends_its_line(self, tmp_path, monkeypatch):
        path = tmp_path / "sherry.jsonc"
        path.write_text('{\n  "LOG_LEVEL": "DEBUG"\n}\n', encoding="utf-8")
        monkeypatch.setattr("server.service.sherry_config.SHERRY_CONFIG_PATH", path)

        write_sherry_config({"TOKEN_BUDGET_DEFAULT_CAP": "250000"})

        text = path.read_text(encoding="utf-8")
        assert '"TOKEN_BUDGET_DEFAULT_CAP": 250000' in text
        assert json5.loads(text)["TOKEN_BUDGET_DEFAULT_CAP"] == 250000
