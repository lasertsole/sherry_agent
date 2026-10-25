"""Per-family repetition-penalty keys in ``.models``: registry, bounds, write.

The panel shows one frequency/presence pair per chat family (main / auxiliary /
TaskFlow / compression / reasoner). Empty is the shipped value and means "use
the global REPETITION_PENALTY default"; a set value must be a finite number in
[-2, 2] or the write is refused before the file is touched.
"""

from __future__ import annotations

import json5
import pytest

from config import models_settings as ms
from server.service import models_config as service

pytestmark = [pytest.mark.unit]

_PREFIXES = ("MAIN_LLM", "AUXILIARY_LLM", "TASKFLOW_LLM", "COMPRESSION_LLM", "REASONER_LLM")
_PENALTY_KEYS = tuple(
    f"{prefix}_{kind}" for prefix in _PREFIXES for kind in ("FREQUENCY_PENALTY", "PRESENCE_PENALTY")
)


def test_every_chat_family_has_the_pair_registered():
    for key in _PENALTY_KEYS:
        assert key in ms.MODEL_SETTING_DEFAULTS
        assert ms.MODEL_SETTING_DEFAULTS[key] == ""


@pytest.mark.parametrize("raw", ["0", "0.3", "-0.25", "2", "-2", "1.75"])
def test_parse_accepts_numbers_inside_the_range(raw):
    for key in _PENALTY_KEYS:
        assert ms.parse_model_value(key, raw) == pytest.approx(float(raw))


@pytest.mark.parametrize("raw", ["-2.01", "2.01", "abc", "nan", "inf", "-inf", "1e999"])
def test_parse_refuses_out_of_range_or_non_finite(raw):
    for key in _PENALTY_KEYS:
        with pytest.raises(ValueError):
            ms.parse_model_value(key, raw)


def test_empty_means_unset_and_renders_as_empty():
    for key in _PENALTY_KEYS:
        assert ms.parse_model_value(key, "") == ""
        assert ms.render_model_value("") == ""


def test_zero_is_rendered_as_a_real_value():
    assert ms.render_model_value(ms.parse_model_value("MAIN_LLM_FREQUENCY_PENALTY", "0")) == "0.0"


def test_write_refuses_an_out_of_range_value(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text('{\n  "MAIN_LLM_FREQUENCY_PENALTY": ""\n}\n', encoding="utf-8")
    monkeypatch.setattr(service, "MODELS_CONFIG_PATH", path)

    with pytest.raises(ValueError, match="between"):
        service.write_models_config({"MAIN_LLM_FREQUENCY_PENALTY": "2.5"})
    # The file is untouched (coercion runs before the backup/write).
    assert json5.loads(path.read_text(encoding="utf-8"))["MAIN_LLM_FREQUENCY_PENALTY"] == ""
    assert not path.with_name(".models.bak").exists()


def test_write_persists_a_valid_value_and_keeps_the_empty_form(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text(
        '{\n  "MAIN_LLM_FREQUENCY_PENALTY": "",\n  "MAIN_LLM_PRESENCE_PENALTY": ""\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "MODELS_CONFIG_PATH", path)

    service.write_models_config(
        {"MAIN_LLM_FREQUENCY_PENALTY": "0.7", "MAIN_LLM_PRESENCE_PENALTY": ""}
    )

    data = json5.loads(path.read_text(encoding="utf-8"))
    assert data["MAIN_LLM_FREQUENCY_PENALTY"] == 0.7
    assert data["MAIN_LLM_PRESENCE_PENALTY"] == ""


def test_read_models_config_shows_the_penalty_fields(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text(
        '{\n  "MAIN_LLM_FREQUENCY_PENALTY": 0.4,\n  "MAIN_LLM_PRESENCE_PENALTY": ""\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "MODELS_CONFIG_PATH", path)

    groups = service.read_models_config()["groups"]
    entries = {entry["key"]: entry["value"] for group in groups for entry in group["entries"]}

    assert entries["MAIN_LLM_FREQUENCY_PENALTY"] == "0.4"
    assert entries["MAIN_LLM_PRESENCE_PENALTY"] == ""


def test_an_older_file_gains_the_keys_after_the_output_cap(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text(
        '{\n  "MAIN_LLM_PROVIDER": "openai",\n'
        '  "MAIN_LLM_MAX_OUTPUT_TOKEN": "",\n'
        '  "AUXILIARY_LLM_MAX_OUTPUT_TOKEN": "",\n'
        '  "REASONER_LLM_MAX_TOKEN": ""\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)

    ms.ensure_models_config_file()

    text = path.read_text(encoding="utf-8")
    data = json5.loads(text)
    for key in _PENALTY_KEYS:
        assert key in data
    # Inserted beside their own family's cap, not appended at the end.
    assert (
        text.index('"MAIN_LLM_MAX_OUTPUT_TOKEN"')
        < text.index('"MAIN_LLM_FREQUENCY_PENALTY"')
        < text.index('"MAIN_LLM_PRESENCE_PENALTY"')
    )
    assert text.index('"REASONER_LLM_MAX_TOKEN"') < text.index('"REASONER_LLM_FREQUENCY_PENALTY"')


def test_the_penalty_append_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text('{\n  "MAIN_LLM_MAX_OUTPUT_TOKEN": ""\n}\n', encoding="utf-8")
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)

    ms.ensure_models_config_file()
    first = path.read_text(encoding="utf-8")
    ms.ensure_models_config_file()

    assert path.read_text(encoding="utf-8") == first
    assert first.count('"MAIN_LLM_FREQUENCY_PENALTY"') == 1


def test_an_older_file_gains_every_chat_family_output_cap(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text(
        '{\n  "MAIN_LLM_MAX_TOKEN": 200000,\n'
        '  "AUXILIARY_LLM_MAX_TOKEN": 200000,\n'
        '  "REASONER_LLM_MAX_TOKEN": ""\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)

    ms.ensure_models_config_file()

    data = json5.loads(path.read_text(encoding="utf-8"))
    assert data["MAIN_LLM_MAX_OUTPUT_TOKEN"] == ""
    assert data["AUXILIARY_LLM_MAX_OUTPUT_TOKEN"] == ""
    assert data["REASONER_LLM_MAX_OUTPUT_TOKEN"] == ""
    # The new key sits before the reasoner's penalty pair (anchor is MAX_TOKEN).
    text = path.read_text(encoding="utf-8")
    assert text.index('"REASONER_LLM_MAX_TOKEN"') < text.index('"REASONER_LLM_MAX_OUTPUT_TOKEN"')
