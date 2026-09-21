import sys
import math
import urllib3
from pathlib import Path
from loguru import logger
from models.utils import read_env_file_value as _read_dotenv
from models.http_client import OpenAICompatibleClient
from langchain_core.embeddings import Embeddings


_MODEL_DIR = Path(__file__).resolve().parent
_WEIGHT_DIR = _MODEL_DIR / "model_weight"


def _abort(msg: str) -> None:
    logger.error(f"[EmbedModel ERROR] {msg}")
    sys.exit(1)


def _detect_backend() -> tuple[str, bool, dict | None]:
    """
    Detect backend mode and return configuration.
    Returns: (backend_type, has_local, remote_config_or_None)
    """
    raw = _read_dotenv("EMBEDDING_MODEL_LOCAL", "true").strip().lower()
    use_local = raw not in ("", "false", "0", "no")

    if use_local:
        return ("local", True, None)

    # Remote mode → validate configuration
    provider = _read_dotenv("EMBEDDING_MODEL_PROVIDER")
    api_base = _read_dotenv("EMBEDDING_API_BASE")
    api_key = _read_dotenv("EMBEDDING_API_KEY")
    api_name = _read_dotenv("EMBEDDING_API_NAME")

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

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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
# ``import models.embed_model.core`` (tests, tooling, type checkers) read .env
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

    Idempotent. The side effects match the former import-time block exactly:
    ``_detect_backend()`` (env read + remote validation + warning suppression)
    runs first, then a local GGUF download is attempted when the weights are
    missing. The download is skipped when llama-cpp-python is absent (hermetic
    CI never installs it): ``_load_model`` retries on first use.
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
# 2. CustomEmbedding class
# ─────────────────────────────────────────────
class CustomEmbedding(Embeddings):
    """Embedding model wrapper (auto-selects local llama.cpp / remote MaaS API)."""

    def _call_remote_api(self, texts: list[str]) -> dict:
        """Call the remote MaaS embedding API."""
        cfg = _remote_config
        client = OpenAICompatibleClient(cfg["api_base"], cfg["api_key"])
        payload = {
            "model": cfg["api_name"],
            "input": texts,
            "encoding_format": "float",
        }
        return client.post_json("/embeddings", payload)

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            return vec
        return [x / norm for x in vec]

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """Local llama.cpp encoding (load on demand, release after use, L2-normalise output)."""
        if not texts:
            return []
        model = _load_model()
        try:
            embeddings = model.create_embedding(input=texts)
            data = embeddings.get("data", [])
            data.sort(key=lambda x: x["index"])
            return [self._l2_normalize(item["embedding"]) for item in data]
        finally:
            model.close()

    def _embed_remote(self, texts: list[str]) -> list[list[float]]:
        """Remote API encoding."""
        result = self._call_remote_api(texts)
        # Return embeddings in input order
        data = result.get("data", [])
        # Each item in data has index and embedding
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for multiple documents."""
        if not texts:
            return []

        _ensure_setup()
        if _backend == "local":
            return self._embed_local(texts)
        return self._embed_remote(texts)

    def embed_query(self, text: str) -> list[float]:
        """Generate an embedding vector for a single query."""
        _ensure_setup()
        if _backend == "local":
            return self._embed_local([text])[0]
        return self._embed_remote([text])[0]


def build_embed_model() -> CustomEmbedding:
    setup_embed_model()
    return CustomEmbedding()
