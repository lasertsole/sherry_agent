"""TDD tests for audit item 1.1.4 — `_read_dotenv` duplicated across
`models/embed_model/text/core.py` and `models/reranker_model/core.py`.

The two copies are byte-identical. Deduplicated into `models/utils.py`,
preserving the documented semantics (parse the .env FILE only — never
os.environ, to avoid load_dotenv side effects).
"""

import pytest


pytestmark = [pytest.mark.module]


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
