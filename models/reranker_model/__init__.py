"""CrossEncoder reranker — local GGUF or cloud API, controlled by ``RERANKER_MODEL_LOCAL``.

``reranker_model`` dispatches to either ``CrossEncoderGGUF`` (local GGUF via
llama-cpp-python) or ``CloudReranker`` (remote API) depending on the
``RERANKER_MODEL_LOCAL`` environment variable.

Exports:
  reranker_model: Unified ``rank()``, ``filter()``, ``predict()`` interface.
  CrossEncoderGGUF: The local GGUF-backed implementation (for advanced usage).
  CloudReranker: The cloud API-backed implementation (for advanced usage).
"""

from __future__ import annotations

from pathlib import Path

from .core import (
    CloudReranker,
    CrossEncoderGGUF,
    _get_meta,
    _is_local,
    _META_CACHE,
)

_MODEL_DIR = Path(__file__).parent.resolve() / "model_weight"
_DEFAULT_MODEL_PATH = str(_MODEL_DIR / "bge-reranker-v2-m3-Q8_0.gguf")


class _LazyReranker:
    """Unified reranker that delegates to the correct backend.

    When ``RERANKER_MODEL_LOCAL=true`` (default):
      - Uses local GGUF model (``CrossEncoderGGUF``)
      - Model loaded per-call and freed after; classifier weights cached.

    When ``RERANKER_MODEL_LOCAL=false``:
      - Uses cloud API (``CloudReranker``)
      - Reads ``RERANKER_API_BASE``, ``RERANKER_API_KEY``, ``RERANKER_API_NAME``
        from ``.env``.
    """

    def __init__(self) -> None:
        self._local = _is_local()
        self._cloud: CloudReranker | None = None
        self._gguf_meta = None

    # ── Meta ──────────────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        """``"gguf"`` when local, ``"cloud"`` when using remote API."""
        return "gguf" if self._local else "cloud"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rank(self, query: str, documents: list[str], **kwargs):
        """Score and rank documents by relevance to *query*."""
        if not documents:
            return []
        if self._local:
            if self._gguf_meta is None:
                self._gguf_meta = _get_meta(_DEFAULT_MODEL_PATH)
            return self._with_gguf("rank", query, documents, **kwargs)
        return self._get_cloud().rank(query, documents, **kwargs)

    def filter(self, query: str, documents: list[str], **kwargs):
        """Filter documents, keeping only those with relevance >= gap_score."""
        if not documents:
            return []
        if self._local:
            if self._gguf_meta is None:
                self._gguf_meta = _get_meta(_DEFAULT_MODEL_PATH)
            return self._with_gguf("filter", query, documents, **kwargs)
        return self._get_cloud().filter(query, documents, **kwargs)

    def predict(self, query: str, passage: str) -> float:
        """Score a single query–passage pair. Returns float in [0, 1]."""
        if self._local:
            if self._gguf_meta is None:
                self._gguf_meta = _get_meta(_DEFAULT_MODEL_PATH)
            return self._with_gguf("predict", query, passage)
        return self._get_cloud().predict(query, passage)

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score a list of (query, passage) pairs."""
        if self._local:
            return [self.predict(q, p) for q, p in pairs]
        return self._get_cloud().predict_scores(pairs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_cloud(self) -> CloudReranker:
        if self._cloud is None:
            self._cloud = CloudReranker()
        return self._cloud

    def _with_gguf(self, method: str, *args, **kwargs):
        """Create CrossEncoderGGUF, call *method*, close it, return result."""
        model = CrossEncoderGGUF(
            model_path=_DEFAULT_MODEL_PATH,
            use_gpu=False,
            _meta=self._gguf_meta,
        )
        try:
            fn = getattr(model, method)
            return fn(*args, **kwargs)
        finally:
            model.close()

    def close(self) -> None:
        """Clear the metadata cache (releases ~16 MB of numpy arrays)."""
        _META_CACHE.clear()
        self._gguf_meta = None


# Singleton instance matching the old `from models.reranker_model import reranker_model`
reranker_model: _LazyReranker = _LazyReranker()

__all__ = [
    "reranker_model",
    "CrossEncoderGGUF",
    "CloudReranker",
]
