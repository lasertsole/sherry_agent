"""TDD tests for audit item 1.1.4 — `_read_dotenv` duplicated across
`models/embed_model/core.py` and `models/reranker_model/core.py`.

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


class TestDedupWired:
    """Both model modules must now use the shared implementation.

    Source-level pin instead of an import: `models/embed_model/core.py`
    downloads the GGUF at import time when the local file is missing
    (pre-existing behavior), so importing it in tests is not possible.
    """

    def test_embed_model_uses_shared(self):
        from pathlib import Path

        src = (
            next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
            / "models"
            / "embed_model"
            / "core.py"
        ).read_text(encoding="utf-8")
        assert "from models.utils import read_env_file_value as _read_dotenv" in src
        assert "def _read_dotenv(" not in src

    def test_reranker_model_uses_shared(self):
        from models import utils as models_utils
        from models.reranker_model import core as reranker_core

        assert reranker_core._read_dotenv is models_utils.read_env_file_value
