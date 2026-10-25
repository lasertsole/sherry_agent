"""``.models`` migration: an existing file gains the optional internal families.

The two families (``TASKFLOW_LLM_*`` / ``COMPRESSION_LLM_*``) shipped after
most ``.models`` files were created, and the 模型配置 panel renders only keys the
file contains — so an existing file must gain the block or the cards never
appear. The append is additive and one-shot.
"""

import json5
import pytest

from config import models_settings as ms

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def existing_models(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    path.write_text('{\n  "MAIN_LLM_PROVIDER": "openai"\n}\n', encoding="utf-8")
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)
    return path


def test_existing_file_gains_both_optional_families(existing_models):
    ms.ensure_models_config_file()

    data = json5.loads(existing_models.read_text(encoding="utf-8"))
    assert data["MAIN_LLM_PROVIDER"] == "openai"
    assert data["TASKFLOW_LLM_PROVIDER"] == ""
    assert data["TASKFLOW_LLM_MAX_TOKEN"] == 131072
    assert data["COMPRESSION_LLM_PROVIDER"] == ""
    assert data["COMPRESSION_LLM_MAX_OUTPUT_TOKEN"] == ""


def test_existing_file_gains_the_pricing_table(existing_models):
    ms.ensure_models_config_file()

    data = json5.loads(existing_models.read_text(encoding="utf-8"))
    assert data["MAIN_LLM_PROVIDER"] == "openai"
    # The flat key/value file stores the table as a JSON STRING; the loader
    # turns it back into an object.
    table = json5.loads(data["MODEL_PRICING_TABLE"])
    assert table["_default"] == {"input": 0.5, "output": 1.5}


def test_the_append_is_idempotent(existing_models):
    ms.ensure_models_config_file()
    first = existing_models.read_text(encoding="utf-8")
    ms.ensure_models_config_file()

    assert existing_models.read_text(encoding="utf-8") == first
    assert first.count('"TASKFLOW_LLM_PROVIDER"') == 1


def test_a_one_line_document_stays_parseable(tmp_path, monkeypatch):
    """A compact ``{ "KEY": value }`` file is valid JSON5 and must survive."""
    path = tmp_path / ".models"
    path.write_text('{ "MAIN_LLM_NAME": "fresh-name" }', encoding="utf-8")
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)

    ms.ensure_models_config_file()

    data = json5.loads(path.read_text(encoding="utf-8"))
    assert data["MAIN_LLM_NAME"] == "fresh-name"
    assert data["TASKFLOW_LLM_MAX_TOKEN"] == 131072
    assert data["COMPRESSION_LLM_MAX_TOKEN"] == 131072


def test_a_partially_present_family_is_not_rewritten(existing_models):
    existing_models.write_text('{\n  "TASKFLOW_LLM_PROVIDER": "openai"\n}\n', encoding="utf-8")

    ms.ensure_models_config_file()

    data = json5.loads(existing_models.read_text(encoding="utf-8"))
    assert data["TASKFLOW_LLM_PROVIDER"] == "openai"
    # The other family still arrives; the partial one is left alone.
    assert "COMPRESSION_LLM_PROVIDER" in data


def test_an_unparseable_file_is_never_rewritten(tmp_path, monkeypatch):
    path = tmp_path / ".models"
    original = '{\n  "MAIN_LLM_PROVIDER": \n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", path)

    ms.ensure_models_config_file()

    assert path.read_text(encoding="utf-8") == original
