from typing import Never
import math
from pathlib import Path
from loguru import logger
from config import APP_HOME, LOG_DIR
from config.features import LOCAL_DAEMONS
from config.models_settings import get_model_setting
from models.daemon.base import (
    DaemonError,
    DaemonSpec,
    ReadyState,
    ensure_ready,
    request_json,
)
from models.http_client import OpenAICompatibleClient
from langchain_core.embeddings import Embeddings


_MODEL_DIR = Path(__file__).resolve().parent
_WEIGHT_DIR = _MODEL_DIR / "model_weight"

# The daemon owns the GGUF; the orchestrator only sends text batches. Ports and
# budgets are central feature config (env-overridable per deployment).
_EMBED_DAEMON = DaemonSpec(
    name="embedding",
    host=LOCAL_DAEMONS["host"],
    port=LOCAL_DAEMONS["embed_port"],
    pid_path=APP_HOME / ".embed_daemon.pid",
    import_fn="models.embed_model.text.daemon",
    ready_wait_s=LOCAL_DAEMONS["embed_ready_wait_s"],
    liveness_timeout_s=LOCAL_DAEMONS["liveness_timeout_s"],
)
_DAEMON_LOG = LOG_DIR / "daemons" / "embed.log"


def _abort(msg: str) -> Never:
    """Fail the embed-model setup with a clear message.

    Raises instead of calling ``sys.exit``: this is a library module, and a
    configuration typo must not kill the whole server process from inside it —
    the caller (``build_embed_model`` / the first embed call) decides, and the
    message still names the missing variable.
    """
    logger.error(f"[EmbedModel ERROR] {msg}")
    raise RuntimeError(f"[EmbedModel] {msg}")


def _detect_backend() -> tuple[str, bool, dict | None]:
    """
    Detect backend mode and return configuration.
    Returns: (backend_type, has_local, remote_config_or_None)
    """
    raw = str(get_model_setting("EMBEDDING_MODEL_LOCAL")).strip().lower()
    use_local = raw not in ("", "false", "0", "no")

    if use_local:
        return ("local", True, None)

    # Remote mode → validate configuration (values come from .models)
    provider = get_model_setting("EMBEDDING_MODEL_PROVIDER")
    api_base = get_model_setting("EMBEDDING_API_BASE")
    api_key = get_model_setting("EMBEDDING_API_KEY")
    api_name = get_model_setting("EMBEDDING_API_NAME")

    if not api_base:
        _abort(
            "EMBEDDING_API_BASE is empty — remote embedding requires API base URL (e.g. https://api.modelarts-maas.com/v1)"
        )
    if not api_key:
        _abort("EMBEDDING_API_KEY is empty — remote embedding requires API Key")
    if not api_name:
        _abort("EMBEDDING_API_NAME is empty — remote embedding requires model name (e.g. bge-m3)")
    if not provider:
        _abort(
            "EMBEDDING_MODEL_PROVIDER is empty — remote embedding requires model provider (e.g. openai)"
        )

    return (
        "remote",
        False,
        {"provider": provider, "api_base": api_base, "api_key": api_key, "api_name": api_name},
    )


# ─────────────────────────────────────────────
# 1. Download helper
# ─────────────────────────────────────────────
def _ensure_downloaded() -> None:
    """Download GGUF to model_weight/ on first run (download only, no model loading)."""
    from llama_cpp import Llama

    if _GGUF_MODEL_PATH.is_file():
        return
    _WEIGHT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading bge-m3-q8_0.gguf to %s ...", _WEIGHT_DIR)
    tmp = Llama.from_pretrained(
        repo_id="ggml-org/bge-m3-Q8_0-GGUF",
        filename="bge-m3-q8_0.gguf",
        local_dir=str(_WEIGHT_DIR),
        embedding=True,
    )
    tmp.close()


def _load_model():
    """Load Llama model and return the instance."""
    from llama_cpp import Llama

    if not _GGUF_MODEL_PATH.is_file():
        _ensure_downloaded()
    return Llama(model_path=str(_GGUF_MODEL_PATH), embedding=True, n_gpu_layers=0, verbose=False)


# ─────────────────────────────────────────────
# 2. Explicit initialisation (idempotent)
# ─────────────────────────────────────────────
# This block used to run at import time, which made any bare
# ``import models.embed_model.text.core`` (tests, tooling, type checkers) read .env
# and trigger an eager GGUF download. It is now an explicit
# ``setup_embed_model()`` invoked by the assembly point (``build_embed_model``)
# and lazily by the first embed call, so importing this module is
# side-effect-free.
_GGUF_MODEL_PATH = _WEIGHT_DIR / "bge-m3-q8_0.gguf"

_backend: str | None = None
_use_local: bool = False
_remote_config: dict | None = None
_setup_done: bool = False


def setup_embed_model() -> None:
    """Detect the backend and eager-download the local GGUF — once.

    Idempotent: ``_detect_backend()`` (env read + remote validation + warning
    suppression) runs first, then a local GGUF download is attempted when the
    weights are missing. The download is skipped when llama-cpp-python is absent
    (hermetic CI never installs it): ``_load_model`` retries on first use.
    """
    global _backend, _use_local, _remote_config, _setup_done
    if _setup_done:
        return
    _backend, _use_local, _remote_config = _detect_backend()
    if _use_local and not _GGUF_MODEL_PATH.is_file():
        try:
            _ensure_downloaded()
        except ModuleNotFoundError:
            logger.warning(
                "llama-cpp-python is not installed; skipping eager GGUF download "
                "(will be attempted on first embed call)"
            )
    _setup_done = True


def _ensure_setup() -> None:
    """Run explicit setup on first use if the factory was bypassed."""
    if not _setup_done:
        setup_embed_model()


# ─────────────────────────────────────────────
# 2. Backend strategies + the stable facade
# ─────────────────────────────────────────────
def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class _LocalEmbeddingBackend:
    """Local bge-m3 strategy: the daemon owns the GGUF, this side normalises.

    Baseline measurement made the daemon path the default: a hot ``Llama`` load
    is ~1.3 s and the RSS peak is ~1 GB, both of which the orchestrator should
    never pay. The daemon keeps one context warm and serialises inference.

    ``EMBEDDING_DAEMON_ENABLED=false`` (or a spawn that cannot start at all)
    restores the historical in-process load/close path, so a deployment that
    cannot fork a daemon keeps semantic recall working.
    """

    def __init__(self) -> None:
        self._daemon_ready = False
        self._daemon_usable = bool(LOCAL_DAEMONS["embed_daemon_enabled"])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._daemon_usable:
            try:
                return self._embed_via_daemon(texts)
            except _DaemonUnavailableError as error:
                logger.warning(
                    "embedding daemon unavailable ({}); falling back to the in-process GGUF "
                    "loader for this process",
                    error,
                )
                self._daemon_usable = False
        return self._embed_in_process(texts)

    def _embed_via_daemon(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._daemon_request(texts)
        except DaemonError:
            # The daemon may have idled out or died between calls; one respawn
            # attempt keeps a 600 s idle window invisible to callers.
            self._daemon_ready = False
            return self._daemon_request(texts)

    def _daemon_request(self, texts: list[str]) -> list[list[float]]:
        if not self._daemon_ready:
            state = ensure_ready(_EMBED_DAEMON, log_path=_DAEMON_LOG)
            if state is ReadyState.UNAVAILABLE:
                raise _DaemonUnavailableError("could not start the embedding daemon")
            if state is not ReadyState.READY:
                raise DaemonError("embedding daemon is still warming up")
            self._daemon_ready = True
        payload = request_json(
            _EMBED_DAEMON,
            "/embeddings",
            {"input": texts},
            timeout_s=LOCAL_DAEMONS["embed_http_timeout_s"],
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise DaemonError("embedding daemon returned a malformed vector batch")
        if not all(
            isinstance(item, dict) and "index" in item and "embedding" in item for item in data
        ):
            raise DaemonError("embedding daemon returned a vector without index/embedding")
        data.sort(key=lambda item: item["index"])
        return [_l2_normalize(item["embedding"]) for item in data]

    def _embed_in_process(self, texts: list[str]) -> list[list[float]]:
        model = _load_model()
        try:
            embeddings = model.create_embedding(input=texts)
            data = embeddings.get("data", [])
            data.sort(key=lambda x: x["index"])
            return [_l2_normalize(item["embedding"]) for item in data]
        finally:
            model.close()


class _DaemonUnavailableError(RuntimeError):
    """No daemon process exists and one could not be started."""


class _RemoteEmbeddingBackend:
    """Remote MaaS API strategy: one HTTP client call per batch."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = OpenAICompatibleClient(self._config["api_base"], self._config["api_key"])
        result = client.post_json(
            "/embeddings",
            {
                "model": self._config["api_name"],
                "input": texts,
                "encoding_format": "float",
            },
        )
        data = result.get("data", [])
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]


def _build_embedding_backend() -> _LocalEmbeddingBackend | _RemoteEmbeddingBackend:
    """Choose the strategy once (the only backend branch left in this module)."""
    _ensure_setup()
    if _backend == "local":
        return _LocalEmbeddingBackend()
    if _remote_config is None:  # defensive: setup validates remote config
        _abort("remote embedding selected but no remote configuration was detected")
    return _RemoteEmbeddingBackend(_remote_config)


class CustomEmbedding(Embeddings):
    """Embedding model facade — delegates to ONE strategy chosen at construction.

    The audit (2026-11-09, M2) removed the per-method ``if _backend == "local"``
    branch; a direct constructor resolves the strategy lazily (so importing or
    constructing without setup stays side-effect free), while ``build_embed_model``
    injects it explicitly.
    """

    def __init__(
        self, backend: _LocalEmbeddingBackend | _RemoteEmbeddingBackend | None = None
    ) -> None:
        self._backend = backend

    def _get_backend(self):
        if self._backend is None:
            self._backend = _build_embedding_backend()
        return self._backend

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for multiple documents."""
        if not texts:
            return []
        return self._get_backend().embed(texts)

    def embed_query(self, text: str) -> list[float]:
        """Generate an embedding vector for a single query."""
        return self._get_backend().embed([text])[0]


def build_embed_model() -> CustomEmbedding:
    setup_embed_model()
    return CustomEmbedding(backend=_build_embedding_backend())
