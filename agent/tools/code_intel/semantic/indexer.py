"""Embedding build + storage over the Phase 1 symbol table.

Incremental by construction: :class:`SemanticIndexer` refreshes the symbol index
first (reusing :class:`~agent.tools.code_intel.indexer.CodeIndexer`), drops
embeddings whose symbol no longer exists (a re-indexed file gets fresh symbol
ids, so its stale rows become orphans), then embeds **only** the symbols without
an embedding. Rows are written in batches and released after each batch — the
local GGUF loader is called at most once per batch, and peak memory stays bounded
by ``batch_size`` chunks plus one batch of vectors.

Every failure mode is fail-open: an unavailable model, a failed batch, a bad
vector or a dimension conflict is reported on :class:`EmbeddingIndexResult` (or
resolved by rebuilding the index) instead of raising. A partially embedded index
is resumable because already-embedded symbols are skipped on the next run.
"""

from __future__ import annotations

import array
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from config.features.agent_side import (
    CODE_INTEL,
    CODE_INTEL_SEMANTIC,
    CodeIntelConfig,
    CodeIntelSemanticConfig,
)

from ..indexer import CodeIndexer
from .chunker import CodeChunk, SymbolRecord, build_chunk

_EMBEDDABLE_KINDS = ("function", "method", "class")

_EMBED_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS code_embeddings (
        symbol_id INTEGER PRIMARY KEY,
        embedding BLOB NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        chunk_text TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (symbol_id) REFERENCES symbols(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_code_emb_symbol ON code_embeddings(symbol_id)",
)

_PENDING_SQL = """
    SELECT s.id, s.name, s.kind, s.file_path, s.line_start, s.line_end,
           s.language, s.doc_string, s.source_snippet
    FROM symbols s
    LEFT JOIN code_embeddings e ON e.symbol_id = s.id
    WHERE e.symbol_id IS NULL AND s.kind IN ({kinds})
    ORDER BY s.file_path, s.line_start
    LIMIT ?
""".format(kinds=",".join(f"'{kind}'" for kind in _EMBEDDABLE_KINDS))


def _pack(vector: list[float]) -> bytes:
    return array.array("d", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array.array("d")
    values.frombytes(blob)
    return values.tolist()


@dataclass(slots=True)
class EmbeddingIndexResult:
    """Outcome of one :meth:`SemanticIndexer.build` invocation."""

    embedded: int = 0
    orphaned: int = 0
    failed: int = 0
    truncated: bool = False
    rebuilt: bool = False
    total: int = 0
    model: str = ""
    dim: int = 0
    elapsed_s: float = 0.0
    error: str | None = None
    errors: list[str] = field(default_factory=list)


class SemanticIndexer:
    """Incremental embedding indexer; fail-open and serialized per instance."""

    def __init__(
        self,
        db_path: str | Path,
        config: CodeIntelConfig | None = None,
        semantic_config: CodeIntelSemanticConfig | None = None,
        *,
        embed_model: object | None = None,
        base_indexer: CodeIndexer | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config or CODE_INTEL
        self._semantic = semantic_config or CODE_INTEL_SEMANTIC
        self._embed_model = embed_model
        self._base_indexer = base_indexer or CodeIndexer(self._db_path, self._config)
        self._lock = threading.Lock()

    # -- infrastructure ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        for ddl in _EMBED_SCHEMA:
            conn.execute(ddl)

    def _get_embed_model(self) -> object:
        if self._embed_model is None:
            from models import build_embed_model

            self._embed_model = build_embed_model()
        return self._embed_model

    # -- public API --------------------------------------------------------

    def build(self, root: str | Path, force: bool = False) -> EmbeddingIndexResult:
        """Refresh the symbol index and embed symbols that lack a vector."""
        with self._lock:
            return self._build(Path(root).resolve(), force)

    # -- build internals ---------------------------------------------------

    def _build(self, root: Path, force: bool) -> EmbeddingIndexResult:
        started = time.monotonic()
        result = EmbeddingIndexResult(model=self._semantic["code_intel_semantic_model"])
        try:
            base = self._base_indexer.index_directory(root, force=force)
            if base.errors:
                # The symbol index still usable for whatever it wrote; record the
                # first error but continue — the semantic layer is additive.
                result.errors.append(f"symbol index: {base.errors[0]}")
        except Exception as exc:  # noqa: BLE001 - index build must never crash a tool call
            result.error = f"symbol index failed: {exc}"
            result.elapsed_s = time.monotonic() - started
            return result

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            result.orphaned = self._drop_orphans(conn)
            if self._invalidate_on_model_change(conn, result.model):
                result.rebuilt = True
            self._embed_pending(conn, root, result)
            result.total = self._count(conn)
            if result.dim == 0:
                result.dim = self._stored_dim(conn)
            if result.embedded == 0 and result.error is None and result.errors:
                # Surface the first batch failure so the search layer can degrade
                # with a concrete reason instead of a generic "no embeddings".
                result.error = result.errors[0]
        except Exception as exc:  # noqa: BLE001 - fail-open per plan
            logger.warning("code_intel.semantic: build failed for {}: {}", root, exc)
            result.error = str(exc)
        finally:
            conn.close()

        result.elapsed_s = time.monotonic() - started
        return result

    def _embed_pending(
        self, conn: sqlite3.Connection, root: Path, result: EmbeddingIndexResult
    ) -> None:
        max_chunks = max(1, int(self._semantic["code_intel_semantic_max_chunks"]))

        try:
            model = self._get_embed_model()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - misconfigured/unavailable model
            result.error = _model_error(exc)
            return

        # Two passes at most: the first can discover a dimension conflict, purge
        # the stale index and ask for a restart; the second rebuilds cleanly.
        for _ in range(2):
            rows = conn.execute(_PENDING_SQL, (max_chunks + 1,)).fetchall()
            if not rows:
                return
            if len(rows) > max_chunks:
                result.truncated = True
                rows = rows[:max_chunks]
            if self._embed_rows(conn, model, rows, root, result):
                result.truncated = False
                continue
            return

    def _embed_rows(
        self,
        conn: sqlite3.Connection,
        model: object,
        rows: list[sqlite3.Row],
        root: Path,
        result: EmbeddingIndexResult,
    ) -> bool:
        """Embed the pending rows; returns True when a purge requires a restart."""
        batch_size = max(1, int(self._semantic["code_intel_semantic_batch_size"]))
        max_per_file = max(1, int(self._semantic["code_intel_semantic_max_chunks_per_file"]))
        max_chars = int(self._semantic["code_intel_semantic_max_chunk_chars"])

        buffer: list[CodeChunk] = []
        current_file: str | None = None
        lines: list[str] | None = None
        file_chunks = 0

        for row in rows:
            if row["file_path"] != current_file:
                current_file = row["file_path"]
                lines = self._read_lines(root / current_file)
                file_chunks = 0
            if file_chunks >= max_per_file:
                continue
            chunk = build_chunk(_record(row), lines, max_chars=max_chars)
            if chunk is None:
                continue
            buffer.append(chunk)
            file_chunks += 1
            if len(buffer) >= batch_size:
                if self._flush(conn, model, buffer, result):
                    return True
                buffer = []

        if buffer and self._flush(conn, model, buffer, result):
            return True
        return False

    def _flush(
        self,
        conn: sqlite3.Connection,
        model: object,
        chunks: list[CodeChunk],
        result: EmbeddingIndexResult,
    ) -> bool:
        """Embed and store one batch; a failure loses only this batch.

        Returns True when a stored dimension conflict purged the index and the
        caller must re-collect the pending set.
        """
        texts = [chunk.text for chunk in chunks]
        try:
            vectors = model.embed_documents(texts)  # type: ignore[attr-defined]
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - one bad batch must not abort
            result.failed += len(chunks)
            result.errors.append(_model_error(exc))
            logger.warning("code_intel.semantic: embed batch of {} failed: {}", len(chunks), exc)
            return False

        if not vectors:
            result.failed += len(chunks)
            result.errors.append("embedding model returned no vectors")
            return False

        dim = len(vectors[0])
        if result.dim == 0:
            if self._invalidate_on_dim_change(conn, dim):
                # Nothing from this batch was written yet; restart to embed every
                # symbol at the new dimension, including the purged ones.
                result.rebuilt = True
                result.dim = dim
                return True
            result.dim = dim

        now = time.time()
        for chunk, vector in zip(chunks, vectors):
            if not vector or len(vector) != dim:
                result.failed += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO code_embeddings"
                "(symbol_id, embedding, model, dim, chunk_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk.symbol_id, _pack(vector), result.model, dim, chunk.text, now),
            )
            result.embedded += 1
        conn.commit()
        del vectors, texts
        return False

    # -- storage helpers ---------------------------------------------------

    @staticmethod
    def _count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM code_embeddings").fetchone()[0])

    @staticmethod
    def _stored_dim(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT dim FROM code_embeddings LIMIT 1").fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _drop_orphans(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "DELETE FROM code_embeddings WHERE symbol_id NOT IN (SELECT id FROM symbols)"
        )
        conn.commit()
        return max(cursor.rowcount, 0)

    @staticmethod
    def _invalidate_on_model_change(conn: sqlite3.Connection, model: str) -> int:
        """Purge the index when any stored row used a different model."""
        models = {row[0] for row in conn.execute("SELECT DISTINCT model FROM code_embeddings")}
        if models and models != {model}:
            removed = conn.execute("DELETE FROM code_embeddings").rowcount
            conn.commit()
            logger.warning(
                "code_intel.semantic: embedding model changed {} -> {}; rebuilt index",
                sorted(models),
                model,
            )
            return max(removed, 0)
        return 0

    @staticmethod
    def _invalidate_on_dim_change(conn: sqlite3.Connection, dim: int) -> int:
        """Purge the index when stored vectors have a different dimension."""
        dims = {int(row[0]) for row in conn.execute("SELECT DISTINCT dim FROM code_embeddings")}
        if dims and dims != {dim}:
            removed = conn.execute("DELETE FROM code_embeddings").rowcount
            conn.commit()
            logger.warning(
                "code_intel.semantic: embedding dimension changed {} -> {}; rebuilt index",
                sorted(dims),
                dim,
            )
            return max(removed, 0)
        return 0

    def _read_lines(self, path: Path) -> list[str] | None:
        """Read a file's lines, respecting the Phase 1 per-file byte ceiling."""
        max_bytes = int(self._config["code_intel_index_max_file_bytes"])
        try:
            if path.stat().st_size > max_bytes:
                return None
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None


def _record(row: sqlite3.Row) -> SymbolRecord:
    return SymbolRecord(
        id=int(row["id"]),
        name=row["name"],
        kind=row["kind"],
        file_path=row["file_path"],
        line_start=int(row["line_start"]),
        line_end=int(row["line_end"]),
        language=row["language"],
        doc_string=row["doc_string"],
        source_snippet=row["source_snippet"],
    )


def _model_error(exc: BaseException) -> str:
    """A short, actionable description of why the embedding model is unusable."""
    if isinstance(exc, ModuleNotFoundError) and "llama_cpp" in str(exc):
        return (
            "embedding model unavailable: llama-cpp-python is not installed for the local "
            "bge-m3 backend; install it or set EMBEDDING_MODEL_LOCAL=false with a remote "
            "EMBEDDING_API_BASE/API_KEY"
        )
    if isinstance(exc, SystemExit):
        return (
            "embedding model unavailable: the EMBEDDING_* configuration is incomplete "
            "(remote mode requires EMBEDDING_API_BASE, EMBEDDING_API_KEY, "
            "EMBEDDING_API_NAME, EMBEDDING_MODEL_PROVIDER)"
        )
    return f"embedding model unavailable: {type(exc).__name__}: {exc}"
