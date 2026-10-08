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


# ---------------------------------------------------------------------------
# Group kinds: the panel renders model groups with their profile manager and
# everything else as a plain key/value card.
# ---------------------------------------------------------------------------

_KIND_ENV_BODY = (
    "MAIN_LLM_NAME = deepseek-flash\n"
    "SHERRY_BROWSER_AGENT_ENABLED = 1\n"
    "SHERRY_BROWSER_HEADLESS = 1\n"
    "SOME_STRAY_KEY = x\n"
)


def test_groups_carry_their_kind_and_the_browser_switches_are_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text(_KIND_ENV_BODY, encoding="utf-8")
    monkeypatch.setattr(env_service, "ENV_PATH", path)

    groups = {group["name"]: group for group in env_service.read_env_file()["groups"]}

    # The browser feature owns a dedicated PLAIN group (not the catch-all).
    assert groups["SHERRY_BROWSER"]["kind"] == "plain"
    assert [entry["key"] for entry in groups["SHERRY_BROWSER"]["entries"]] == [
        "SHERRY_BROWSER_AGENT_ENABLED",
        "SHERRY_BROWSER_HEADLESS",
    ]
    # A model group keeps its profile manager; the catch-all stays plain.
    assert groups["MAIN_LLM"]["kind"] == "model"
    assert groups["other"]["kind"] == "plain"
    assert [entry["key"] for entry in groups["other"]["entries"]] == ["SOME_STRAY_KEY"]
