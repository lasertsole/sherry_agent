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

from loguru import logger

from config import APP_HOME, LOG_DIR
from config.features import LOCAL_DAEMONS
from models.daemon.base import (
    DaemonError,
    DaemonSpec,
    ReadyState,
    ensure_ready,
    request_json,
)

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

# The daemon owns the GGUF; the orchestrator sends a query + documents and gets
# scores back. Ports and budgets are central feature config.
_RERANKER_DAEMON = DaemonSpec(
    name="reranker",
    host=LOCAL_DAEMONS["host"],
    port=LOCAL_DAEMONS["reranker_port"],
    pid_path=APP_HOME / ".reranker_daemon.pid",
    import_fn="models.reranker_model.daemon",
    ready_wait_s=LOCAL_DAEMONS["reranker_ready_wait_s"],
    liveness_timeout_s=LOCAL_DAEMONS["liveness_timeout_s"],
)
_DAEMON_LOG = LOG_DIR / "daemons" / "reranker.log"


class _RerankerDaemonUnavailableError(RuntimeError):
    """No daemon process exists and one could not be started."""


class _LocalGgufBackend:
    """Local GGUF strategy: a warm daemon, with the in-process loader as fallback.

    The daemon keeps one ``CrossEncoderGGUF`` alive; the in-process path (load +
    metadata cache + close per call) stays behind ``RERANKER_DAEMON_ENABLED=false``
    and as the fallback when no daemon process can be started at all.
    """

    label = "gguf"

    def __init__(self) -> None:
        self._meta = None
        self._daemon_ready = False
        self._daemon_usable = bool(LOCAL_DAEMONS["reranker_daemon_enabled"])

    # ---- daemon path -------------------------------------------------

    def _request(self, path: str, payload: dict) -> dict:
        if not self._daemon_ready:
            state = ensure_ready(_RERANKER_DAEMON, log_path=_DAEMON_LOG)
            if state is ReadyState.UNAVAILABLE:
                raise _RerankerDaemonUnavailableError("could not start the reranker daemon")
            if state is not ReadyState.READY:
                raise DaemonError("reranker daemon is still warming up")
            self._daemon_ready = True
        body = request_json(
            _RERANKER_DAEMON,
            path,
            payload,
            timeout_s=LOCAL_DAEMONS["reranker_http_timeout_s"],
        )
        if not isinstance(body, dict):
            raise DaemonError("reranker daemon returned a malformed body")
        return body

    def _daemon_call(self, path: str, payload: dict, key: str):
        try:
            body = self._request(path, payload)
        except DaemonError:
            # An idle-exit or crash between calls: one respawn attempt.
            self._daemon_ready = False
            body = self._request(path, payload)
        if not isinstance(body, dict) or key not in body:
            raise DaemonError(f"reranker daemon returned a body without {key!r}")
        return body[key]

    def _with_fallback(self, path: str, payload: dict, key: str, method: str, *args, **kwargs):
        if not self._daemon_usable:
            return self._in_process(method, *args, **kwargs)
        try:
            return self._daemon_call(path, payload, key)
        except _RerankerDaemonUnavailableError as error:
            logger.warning(
                "reranker daemon unavailable ({}); falling back to the in-process GGUF "
                "loader for this process",
                error,
            )
            self._daemon_usable = False
            return self._in_process(method, *args, **kwargs)

    # ---- in-process fallback ----------------------------------------

    def _in_process(self, method: str, *args, **kwargs):
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

    # ---- public API --------------------------------------------------

    def rank(self, query: str, documents: list[str], **kwargs):
        return self._with_fallback(
            "/rerank",
            {
                "query": query,
                "documents": documents,
                "top_k": kwargs.get("top_k"),
                "gap_score": kwargs.get("gap_score"),
            },
            "result",
            "rank",
            query,
            documents,
            **kwargs,
        )

    def filter(self, query: str, documents: list[str], **kwargs):
        return self._with_fallback(
            "/filter",
            {
                "query": query,
                "documents": documents,
                "gap_score": kwargs.get("gap_score", 0.5),
            },
            "result",
            "filter",
            query,
            documents,
            **kwargs,
        )

    def predict(self, query: str, passage: str) -> float:
        scores = self.predict_scores([(query, passage)])
        return scores[0]

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        return self._with_fallback(
            "/predict",
            {"pairs": [[query, passage] for query, passage in pairs]},
            "scores",
            "predict_scores",
            pairs,
        )

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
