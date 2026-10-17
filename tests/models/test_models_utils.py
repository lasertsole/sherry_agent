"""TDD tests for audit item 1.1.4 — `_read_dotenv` duplicated across
`models/embed_model/text/core.py` and `models/reranker_model/core.py`.

The two copies are byte-identical. Deduplicated into `models/utils.py`,
preserving the documented semantics (parse the .env FILE only — never
os.environ, to avoid load_dotenv side effects).
"""

import pytest

from models.utils import read_env_file_value


pytestmark = [pytest.mark.module]


@pytest.fixture()
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "\n".join(
            [
                "# comment",
                "SIMPLE=value1",
                'QUOTED="quoted value"',
                "EXPORTED=exported_value",
                "SPACED =  spaced value  ",
                "EMPTY=",
                "EMBEDDING_MODEL_LOCAL=false",
            ]
        ),
        encoding="utf-8",
    )
    return p


class TestReadEnvFileValue:
    def test_reads_simple_value(self, env_file):
        assert read_env_file_value("SIMPLE", env_path=env_file) == "value1"

    def test_strips_quotes(self, env_file):
        assert read_env_file_value("QUOTED", env_path=env_file) == "quoted value"

    def test_supports_export_prefix(self, env_file):
        assert read_env_file_value("EXPORTED", env_path=env_file) == "exported_value"

    def test_strips_surrounding_whitespace(self, env_file):
        assert read_env_file_value("SPACED", env_path=env_file) == "spaced value"

    def test_missing_key_returns_default(self, env_file):
        assert read_env_file_value("ABSENT", "fallback", env_path=env_file) == "fallback"

    def test_empty_value_returns_default(self, env_file):
        assert read_env_file_value("EMPTY", "fallback", env_path=env_file) == "fallback"

    def test_missing_file_returns_default(self, tmp_path):
        assert read_env_file_value("ANY", "fallback", env_path=tmp_path / "nope.env") == "fallback"

    def test_first_match_wins(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("K=first\nK=second\n", encoding="utf-8")
        assert read_env_file_value("K", env_path=p) == "first"


class TestConfigWiring:
    """Both model modules read EMBEDDING_* / RERANKER_* from ``.models``.

    The keys moved out of ``.env`` (the duplicated ``_read_dotenv`` helpers and
    the shared ``models.utils`` reader they pointed at are no longer the
    production path — ``config/models_settings.get_model_setting`` is), so these
    pins assert the new wiring.
    """

    def test_embed_model_reads_models_config(self):
        from pathlib import Path

        src = (
            next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
            / "models"
            / "embed_model"
            / "text"
            / "core.py"
        ).read_text(encoding="utf-8")
        assert "from config.models_settings import get_model_setting" in src
        assert "def _read_dotenv(" not in src

    def test_reranker_model_reads_models_config(self, monkeypatch):
        import config.models_settings as models_settings
        from models.reranker_model import core as reranker_core

        monkeypatch.delenv("RERANKER_MODEL_LOCAL", raising=False)
        monkeypatch.setattr(
            models_settings,
            "load_model_settings",
            lambda: {**models_settings.MODEL_SETTING_DEFAULTS, "RERANKER_MODEL_LOCAL": False},
        )

        # False comes from .models, not from a stale environment entry.
        assert reranker_core._is_local() is False
