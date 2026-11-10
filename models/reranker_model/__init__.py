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
    RerankerProtocol,
    _get_meta,
    _is_local,
    _META_CACHE,
    reranker_conformance,
)

_MODEL_DIR = Path(__file__).parent.resolve() / "model_weight"
_DEFAULT_MODEL_PATH = str(_MODEL_DIR / "bge-reranker-v2-m3-Q8_0.gguf")


class _LocalGgufBackend:
    """Local GGUF strategy: loads the model per call, caches its metadata."""

    label = "gguf"

    def __init__(self) -> None:
        self._meta = None

    def _call(self, method: str, *args, **kwargs):
        if self._meta is None:
            self._meta = _get_meta(_DEFAULT_MODEL_PATH)
        model = CrossEncoderGGUF(
            model_path=_DEFAULT_MODEL_PATH,
            use_gpu=False,
            _meta=self._meta,
        )
        try:
            return getattr(model, method)(*args, **kwargs)
        finally:
            model.close()

    def rank(self, query: str, documents: list[str], **kwargs):
        return self._call("rank", query, documents, **kwargs)

    def filter(self, query: str, documents: list[str], **kwargs):
        return self._call("filter", query, documents, **kwargs)

    def predict(self, query: str, passage: str) -> float:
        return self._call("predict", query, passage)

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [self.predict(query, passage) for query, passage in pairs]

    def close(self) -> None:
        self._meta = None


class _CloudBackend:
    """Remote API strategy: one lazily-built CloudReranker per process."""

    label = "cloud"

    def __init__(self) -> None:
        self._client: CloudReranker | None = None

    def _get(self) -> CloudReranker:
        if self._client is None:
            self._client = CloudReranker()
        return self._client

    def rank(self, query: str, documents: list[str], **kwargs):
        return self._get().rank(query, documents, **kwargs)

    def filter(self, query: str, documents: list[str], **kwargs):
        return self._get().filter(query, documents, **kwargs)

    def predict(self, query: str, passage: str) -> float:
        return self._get().predict(query, passage)

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        return self._get().predict_scores(pairs)

    def close(self) -> None:
        self._client = None


class _LazyReranker:
    """Unified reranker that delegates to ONE strategy chosen at construction.

    When ``RERANKER_MODEL_LOCAL=true`` (default) the strategy is
    :class:`_LocalGgufBackend` (model loaded per-call and freed; weights cached).
    When false it is :class:`_CloudBackend` (remote API, lazily built). Callers
    keep one stable surface and never branch on the backend again.
    """

    def __init__(self) -> None:
        self._backend: _LocalGgufBackend | _CloudBackend = (
            _LocalGgufBackend() if _is_local() else _CloudBackend()
        )

    @property
    def backend(self) -> str:
        """``"gguf"`` when local, ``"cloud"`` when using remote API."""
        return self._backend.label

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rank(self, query: str, documents: list[str], **kwargs):
        """Score and rank documents by relevance to *query*."""
        if not documents:
            return []
        return self._backend.rank(query, documents, **kwargs)

    def filter(self, query: str, documents: list[str], **kwargs):
        """Filter documents, keeping only those with relevance >= gap_score."""
        if not documents:
            return []
        return self._backend.filter(query, documents, **kwargs)

    def predict(self, query: str, passage: str) -> float:
        """Score a single query–passage pair. Returns float in [0, 1]."""
        return self._backend.predict(query, passage)

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score a list of (query, passage) pairs."""
        return self._backend.predict_scores(pairs)

    def close(self) -> None:
        """Clear the metadata cache (releases ~16 MB of numpy arrays)."""
        _META_CACHE.clear()
        self._backend.close()


def build_reranker_model() -> _LazyReranker:
    """Create a fresh lazy reranker (backends still load on first use)."""
    return _LazyReranker()


# Singleton instance matching the old `from models.reranker_model import reranker_model`
reranker_model: _LazyReranker = build_reranker_model()

__all__ = [
    "reranker_model",
    "build_reranker_model",
    "CrossEncoderGGUF",
    "CloudReranker",
    "RerankerProtocol",
    "reranker_conformance",
]
