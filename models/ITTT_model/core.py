"""ITTT_model — auto-selects between remote API and local GGUF (multimodal).

If ``ITTT_MODEL_LOCAL=true`` is set in ``.env``, uses the local GGUF model
(``Qwen3.5-9B-Q4_K_M.gguf`` + ``mmproj-Qwen3.5-9B-BF16.gguf`` for multimodal vision support).

Otherwise uses the remote API via ``init_chat_model()`` (legacy behaviour).

Usage:
    from models import ITTT_model
    from langchain_core.messages import HumanMessage

    # Text-only
    result = ITTT_model.invoke("Describe the image")

    # Multimodal (image_url)
    result = ITTT_model.invoke([HumanMessage(content=[
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    ])])
"""

import os
from pathlib import Path

from loguru import logger

from config import ENV_PATH
from config.features import LLM_CLIENT_DEFAULTS
from typing import Any
from dotenv import load_dotenv
from models.LLMs.base_local_llama import LocalMultimodalLlamaChatBase
from models.env_builder import ModelEnvBuilder
from langchain_core.runnables import ConfigurableField

# ---------------------------------------------------------------------------
# 1.  Locate model weight directory & read env
# ---------------------------------------------------------------------------

# .env is loaded from project root via config.path (ENV_PATH)
load_dotenv(ENV_PATH, override=True)

# ITTT_model weight directory (side-by-side with this file)
_MODEL_WEIGHT_DIR = Path(__file__).parent.resolve() / "model_weight"

# ---------------------------------------------------------------------------
# 2.  Decide remote vs. local
# ---------------------------------------------------------------------------

_is_local = os.getenv("ITTT_MODEL_LOCAL", "").strip().lower() == "true"

if not _is_local:
    # ======================== Remote (API) branch ========================
    from langchain.chat_models import init_chat_model

    _model_config: dict[str, Any] = ModelEnvBuilder(
        {
            "model_provider": "ITTT_MODEL_PROVIDER",
            "model": "ITTT_API_NAME",
            "api_key": "ITTT_API_KEY",
            "base_url": "ITTT_API_BASE",
        }
    ).build(
        {
            "temperature": LLM_CLIENT_DEFAULTS["ittt_remote_temperature"],
            "max_retries": LLM_CLIENT_DEFAULTS["ittt_remote_max_retries"],
            # Explicit bounded window for each remote request (seconds).
            "timeout": LLM_CLIENT_DEFAULTS["ittt_remote_timeout"],
        }
    )

    if not _model_config:
        # No ITTT_* configuration at all (e.g. hermetic CI: no .env, remote
        # default). init_chat_model would raise on an empty config, so leave
        # the model unbuilt; it is only needed when _vision_model_func (or a
        # user) actually invokes it on a configured environment.
        logger.warning("no ITTT_* configuration found; model left unbuilt")
        ITTT_model = None
    else:
        ITTT_model = init_chat_model(**_model_config).configurable_fields(
            temperature=ConfigurableField(id="temperature"),
        )

else:
    # ======================== Local (GGUF) branch ========================

    _GGUF_FILENAME = "Qwen3.5-9B-Q4_K_M.gguf"
    _MMPROJ_FILENAME = "mmproj-Qwen3.5-9B-BF16.gguf"
    _HF_REPO_ID = "lmstudio-community/Qwen3.5-9B-GGUF"

    _gguf_path = _MODEL_WEIGHT_DIR / _GGUF_FILENAME
    _mmproj_path = _MODEL_WEIGHT_DIR / _MMPROJ_FILENAME

    # Fallback: check auxiliary_llm's model_weight directory (may already exist)
    _aux_model_weight = (
        Path(__file__).parent.parent.resolve() / "LLMs" / "auxiliary_llm" / "model_weight"
    )
    _fallback_gguf_path = _aux_model_weight / _GGUF_FILENAME

    # ------------------------------------------------------------------
    # 2a.  Helper: resolve model file path (download from HF if missing)
    # ------------------------------------------------------------------

    def _resolve_model_path() -> str:
        """Local GGUF path: local hit -> auxiliary fallback copy -> HF download."""
        from models.utils import resolve_gguf_path

        return resolve_gguf_path(
            _gguf_path,
            _HF_REPO_ID,
            _GGUF_FILENAME,
            _MODEL_WEIGHT_DIR,
            fallback_path=_fallback_gguf_path,
        )

    # ------------------------------------------------------------------
    # 2b.  Helper: resolve mmproj path (download if missing)
    # ------------------------------------------------------------------

    def _resolve_mmproj_path() -> str:
        """Local mmproj path, downloading from HF if needed."""
        from models.utils import resolve_gguf_path

        return resolve_gguf_path(_mmproj_path, _HF_REPO_ID, _MMPROJ_FILENAME, _MODEL_WEIGHT_DIR)

    # ------------------------------------------------------------------
    # 2c.  LocalLlamaChatModel — LangChain wrapper around llama_cpp.Llama
    #      with Qwen25VLChatHandler for multimodal vision support
    # ------------------------------------------------------------------

    class LocalLlamaChatModel(LocalMultimodalLlamaChatBase):
        """ITTT variant: the shared multimodal conversion lives on the base
        (audit 5.2); only model resolution is module-specific."""

        n_ctx: int = 8192

        def _resolve_model_path(self) -> str:
            return _resolve_model_path()

        def _resolve_mmproj_path(self) -> str:
            return _resolve_mmproj_path()

        @property
        def _llm_type(self) -> str:
            return "local-llama-cpp-multimodal"

    # ------------------------------------------------------------------
    # 2e.  Instantiate the singleton
    # ------------------------------------------------------------------

    ITTT_model = LocalLlamaChatModel().configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )
