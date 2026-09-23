"""Unit tests for the incremental embedding indexer (fake embedding backend)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.tools.code_intel.semantic import SemanticIndexer
from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeIntelConfig, CodeIntelSemanticConfig

from .conftest import FakeEmbedding, FailingEmbedding

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def semantic_config() -> CodeIntelSemanticConfig:
    return CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)


def _indexer(
    index_db: Path, intel_config: CodeIntelConfig, semantic_config, model: Any
) -> SemanticIndexer:
    return SemanticIndexer(str(index_db), intel_config, semantic_config, embed_model=model)


def _embedded_ids(db_rows) -> set[int]:
    return {int(row["symbol_id"]) for row in db_rows("SELECT symbol_id FROM code_embeddings")}


def _symbols_in_file(db_rows, path: str, kinds=("function", "method", "class")) -> set[int]:
    placeholders = ",".join("?" * len(kinds))
    rows = db_rows(
        f"SELECT id FROM symbols WHERE file_path = ? AND kind IN ({placeholders})",
        (path, *kinds),
    )
    return {int(row["id"]) for row in rows}


def test_build_embeds_every_embeddable_symbol(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config, db_rows
) -> None:
    fake = FakeEmbedding()
    result = _indexer(index_db, intel_config, semantic_config, fake).build(sample_repo)

    assert result.error is None
    assert result.embedded > 0
    assert result.total == result.embedded
    assert result.dim == fake.dim

    rows = db_rows("SELECT model, dim, chunk_text, created_at FROM code_embeddings LIMIT 1")
    assert rows[0]["model"] == semantic_config["code_intel_semantic_model"]
    assert rows[0]["dim"] == fake.dim
    assert rows[0]["chunk_text"]
    assert rows[0]["created_at"] > 0


def test_second_build_is_incremental(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    fake = FakeEmbedding()
    indexer = _indexer(index_db, intel_config, semantic_config, fake)

    first = indexer.build(sample_repo)
    calls_after_first = fake.document_calls
    second = indexer.build(sample_repo)

    assert second.embedded == 0
    assert second.total == first.total
    assert fake.document_calls == calls_after_first  # no re-embedding of unchanged symbols


def test_changed_file_reembeds_only_that_file(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config, db_rows
) -> None:
    fake = FakeEmbedding()
    indexer = _indexer(index_db, intel_config, semantic_config, fake)
    indexer.build(sample_repo)

    untouched_before = _symbols_in_file(db_rows, "web/app.ts")
    assert untouched_before and untouched_before <= _embedded_ids(db_rows)

    target = sample_repo / "pkg" / "main.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef brand_new(x):\n    return x * 2\n",
        encoding="utf-8",
    )

    result = indexer.build(sample_repo)

    assert result.embedded >= 1
    untouched_after = _symbols_in_file(db_rows, "web/app.ts")
    assert untouched_after == untouched_before
    assert untouched_after <= _embedded_ids(db_rows)  # unchanged file not re-embedded


def test_deleted_file_orphans_are_removed(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config, db_rows
) -> None:
    fake = FakeEmbedding()
    indexer = _indexer(index_db, intel_config, semantic_config, fake)
    indexer.build(sample_repo)

    (sample_repo / "chain" / "level3.py").unlink()
    result = indexer.build(sample_repo)

    assert result.orphaned >= 1
    remaining = _embedded_ids(db_rows)
    live_symbols = {int(row["id"]) for row in db_rows("SELECT id FROM symbols")}
    assert remaining <= live_symbols


def test_model_change_rebuilds_the_index(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, db_rows
) -> None:
    fake = FakeEmbedding()
    config_a = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    config_a["code_intel_semantic_model"] = "model-a"
    _indexer(index_db, intel_config, config_a, fake).build(sample_repo)

    config_b = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    config_b["code_intel_semantic_model"] = "model-b"
    result = _indexer(index_db, intel_config, config_b, fake).build(sample_repo)

    assert result.rebuilt is True
    assert result.error is None
    models = {row["model"] for row in db_rows("SELECT DISTINCT model FROM code_embeddings")}
    assert models == {"model-b"}


def test_dimension_change_rebuilds_the_index_and_restarts(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, db_rows
) -> None:
    partial = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    partial["code_intel_semantic_max_chunks"] = 3
    first = _indexer(index_db, intel_config, partial, FakeEmbedding(dim=8)).build(sample_repo)
    assert first.total == 3

    full = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    result = _indexer(index_db, intel_config, full, FakeEmbedding(dim=16)).build(sample_repo)

    assert result.rebuilt is True
    assert result.error is None
    dims = {int(row["dim"]) for row in db_rows("SELECT DISTINCT dim FROM code_embeddings")}
    assert dims == {16}
    # the purge triggers a restart, so the previously dim-8-only symbols get re-embedded
    assert result.total > 3


def test_unavailable_model_is_fail_open(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    result = _indexer(index_db, intel_config, semantic_config, FailingEmbedding()).build(
        sample_repo
    )

    assert result.error is not None
    assert result.embedded == 0
    assert result.failed > 0
    assert result.errors


def test_syntax_error_file_is_skipped(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config, db_rows
) -> None:
    fake = FakeEmbedding()
    result = _indexer(index_db, intel_config, semantic_config, fake).build(sample_repo)

    assert result.error is None
    broken = db_rows("SELECT COUNT(*) AS n FROM symbols WHERE file_path = 'pkg/broken.py'")
    assert broken[0]["n"] == 0


def test_max_chunks_truncates_the_build(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    semantic_config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    semantic_config["code_intel_semantic_max_chunks"] = 2
    result = _indexer(index_db, intel_config, semantic_config, FakeEmbedding()).build(sample_repo)

    assert result.embedded <= 2
    assert result.truncated is True


def test_batch_size_does_not_drop_rows(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    semantic_config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    semantic_config["code_intel_semantic_batch_size"] = 1
    result = _indexer(index_db, intel_config, semantic_config, FakeEmbedding()).build(sample_repo)

    assert result.embedded > 3
    assert result.total == result.embedded


def test_empty_repo_embeds_nothing(
    tmp_path: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _indexer(index_db, intel_config, semantic_config, FakeEmbedding()).build(empty)

    assert result.embedded == 0
    assert result.total == 0
    assert result.error is None
