"""Unit contract for ``write_env_file()`` MAX_TOKEN validation.

A sub-128K token value must never reach disk: the write is rejected with
``ValueError`` before the file is touched (the HTTP handler turns that into
``{"success": False}`` for the client).
"""

from pathlib import Path

import pytest

from config.features import MIN_REQUIRED_MAX_TOKEN
from server.service import env as env_service

pytestmark = [pytest.mark.unit]

_ENV_BODY = (
    "MAIN_LLM_MAX_TOKEN = 131072\nAUXILIARY_LLM_MAX_TOKEN = 131072\nMAIN_LLM_NAME = test-model\n"
)


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    path.write_text(_ENV_BODY, encoding="utf-8")
    monkeypatch.setattr(env_service, "ENV_PATH", path)
    return path


def test_main_below_minimum_rejected_without_writing(env_file: Path) -> None:
    with pytest.raises(ValueError, match=f"must be >= {MIN_REQUIRED_MAX_TOKEN}"):
        env_service.write_env_file({"MAIN_LLM_MAX_TOKEN": "65536"})

    assert env_file.read_text(encoding="utf-8") == _ENV_BODY


def test_auxiliary_below_minimum_rejected_without_writing(env_file: Path) -> None:
    with pytest.raises(ValueError, match=f"must be >= {MIN_REQUIRED_MAX_TOKEN}"):
        env_service.write_env_file({"AUXILIARY_LLM_MAX_TOKEN": "65536"})

    assert env_file.read_text(encoding="utf-8") == _ENV_BODY


def test_non_integer_token_rejected(env_file: Path) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        env_service.write_env_file({"AUXILIARY_LLM_MAX_TOKEN": "abc"})

    assert env_file.read_text(encoding="utf-8") == _ENV_BODY


def test_valid_token_value_is_written(env_file: Path) -> None:
    env_service.write_env_file({"MAIN_LLM_MAX_TOKEN": "262144"})

    assert "MAIN_LLM_MAX_TOKEN = 262144" in env_file.read_text(encoding="utf-8")


def test_non_token_key_is_not_guarded(env_file: Path) -> None:
    env_service.write_env_file({"MAIN_LLM_NAME": "other-model"})

    assert "MAIN_LLM_NAME = other-model" in env_file.read_text(encoding="utf-8")
