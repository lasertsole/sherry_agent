"""GGUF-based CrossEncoder reranker — replaces the old PyTorch-based reranker_model.

The model weight is loaded on-demand when ``rank()``, ``filter()``, or
``predict()`` is called, and **unloaded** immediately after the method
returns.  Only the classifier head and tokenizer constants (~16 MB of
numpy arrays) are cached in memory.

Exports:
  reranker_model: Lazy per-call CrossEncoderGGUF instance with rank() and filter() API.
  CrossEncoderGGUF: The underlying class (for advanced usage).
"""

from __future__ import annotations

from pathlib import Path

from .core import CrossEncoderGGUF, _get_meta, _META_CACHE

_MODEL_DIR = Path(__file__).parent.resolve() / "model_weight"
_DEFAULT_MODEL_PATH = str(_MODEL_DIR / "bge-reranker-v2-m3-Q8_0.gguf")


class _LazyReranker:
    """CrossEncoder wrapper that loads the model per-call and unloads after.

    Only the classifier weights and token constants (~16 MB) are cached in
    ``_META_CACHE`` — the 636 MB llama.cpp model is created, used, and
    freed for every public API call.

    Thread-safety: each method call is self-contained (no mutable shared
    state beyond the read-only meta cache).
    """

    # ── Meta ──────────────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        """Always ``"gguf"`` — this implementation uses llama-cpp-python / GGUF."""
        return "gguf"

    # ------------------------------------------------------------------
    # Public API — load → call → unload
    # ------------------------------------------------------------------

    def rank(self, query: str, documents: list[str], **kwargs):
        """Load model, rank documents, unload model."""
        if not documents:
            return []
        meta = _get_meta(_DEFAULT_MODEL_PATH)
        return self._with_model(meta, "rank", query, documents, **kwargs)

    def filter(self, query: str, documents: list[str], **kwargs):
        """Load model, filter documents, unload model."""
        if not documents:
            return []
        meta = _get_meta(_DEFAULT_MODEL_PATH)
        return self._with_model(meta, "filter", query, documents, **kwargs)

    def predict(self, query: str, passage: str) -> float:
        """Load model, score pair, unload model."""
        meta = _get_meta(_DEFAULT_MODEL_PATH)
        return self._with_model(meta, "predict", query, passage)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _with_model(self, meta, method: str, *args, **kwargs):
        """Create CrossEncoderGGUF, call *method* on it, close it, return result."""
        model = CrossEncoderGGUF(
            model_path=_DEFAULT_MODEL_PATH,
            use_gpu=False,
            _meta=meta,
        )
        try:
            fn = getattr(model, method)
            return fn(*args, **kwargs)
        finally:
            model.close()

    def close(self) -> None:
        """Clear the metadata cache (releases ~16 MB of numpy arrays)."""
        _META_CACHE.clear()


# Singleton instance matching the old `from models.reranker_model import reranker_model`
reranker_model: _LazyReranker = _LazyReranker()

__all__ = [
    "reranker_model",
    "CrossEncoderGGUF",
]
