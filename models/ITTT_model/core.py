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
from typing import Any
from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
)
from models.LLMs.base_local_llama import LocalMultimodalLlamaChatBase
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

    _api_key = os.getenv("ITTT_API_KEY", "").strip() or None
    _api_name = os.getenv("ITTT_API_NAME", "").strip() or None
    _api_base = os.getenv("ITTT_API_BASE", "").strip() or None
    _provider = os.getenv("ITTT_MODEL_PROVIDER", "").strip() or None

    _model_config: dict[str, Any] = {
        "model_provider": _provider,
        "model": _api_name,
        "api_key": _api_key,
        "base_url": _api_base,
        "temperature": 0.8,
        "max_retries": 2,
    }
    _model_config = {k: v for k, v in _model_config.items() if v is not None and v != ""}

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
    # 2c.  Message converter (supports multimodal HumanMessage)
    # ------------------------------------------------------------------

    def _convert_message_to_dict_impl(message: BaseMessage) -> dict[str, Any]:
        """Convert a LangChain ``BaseMessage`` to the dict expected by
        ``llama_cpp.Llama.create_chat_completion()``.

        Handles both plain-text messages and multimodal messages where
        ``HumanMessage.content`` is a list of content blocks (text + image_url).
        """
        role: str
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            role = "user"

        content = message.content

        # --- Multimodal HumanMessage: content is a list of blocks ---
        if isinstance(content, list):
            converted: list[dict[str, Any]] = []
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        converted.append({"type": "text", "text": block.get("text", "")})
                    elif block_type == "image_url":
                        url = block.get("image_url", {})
                        if isinstance(url, dict):
                            converted.append({"type": "image_url", "image_url": url.get("url", "")})
                        else:
                            converted.append({"type": "image_url", "image_url": url})
                    else:
                        # Pass unknown blocks as-is
                        converted.append(block)
                else:
                    converted.append(block)
            return {"role": role, "content": converted}

        # --- Plain-text message ---
        return {"role": role, "content": str(content) if content is not None else ""}

    # ------------------------------------------------------------------
    # 2d.  LocalLlamaChatModel — LangChain wrapper around llama_cpp.Llama
    #      with Qwen25VLChatHandler for multimodal vision support
    # ------------------------------------------------------------------

    class LocalLlamaChatModel(LocalMultimodalLlamaChatBase):
        """ITTT variant: delegates conversion/resolution to this
        module's closures; lifecycle/fields live on the multimodal base
        (audit 1.1.1)."""

        n_ctx: int = 8192

        def _resolve_model_path(self) -> str:
            return _resolve_model_path()

        def _resolve_mmproj_path(self) -> str:
            return _resolve_mmproj_path()

        def _convert_message_to_dict(self, message: BaseMessage) -> dict[str, Any]:
            return _convert_message_to_dict_impl(message)

        @property
        def _llm_type(self) -> str:
            return "local-llama-cpp-multimodal"

    # ------------------------------------------------------------------
    # 2e.  Instantiate the singleton
    # ------------------------------------------------------------------

    ITTT_model = LocalLlamaChatModel().configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )
