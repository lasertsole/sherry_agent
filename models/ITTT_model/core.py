"""ITTT_model — auto-selects between remote API and local GGUF (multimodal).

If ``ITTT_MODEL_LOCAL=true`` is set in ``.env``, uses the local GGUF model
(``Qwen3.5-9B-q4_k_m.gguf`` + ``mmproj-f16.gguf`` for multimodal vision support).

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
import atexit
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from config import ENV_PATH
from dotenv import load_dotenv
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
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

    ITTT_model = init_chat_model(**_model_config).configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )

else:
    # ======================== Local (GGUF) branch ========================
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import Qwen25VLChatHandler

    _GGUF_FILENAME = "Qwen3.5-9B-Q4_K_M.gguf"
    _MMPROJ_FILENAME = "mmproj-Qwen3.5-9B-BF16.gguf"
    _HF_REPO_ID = "lmstudio-community/Qwen3.5-9B-GGUF"

    _gguf_path = _MODEL_WEIGHT_DIR / _GGUF_FILENAME
    _mmproj_path = _MODEL_WEIGHT_DIR / _MMPROJ_FILENAME

    # Fallback: check auxiliary_llm's model_weight directory (may already exist)
    _aux_model_weight = Path(__file__).parent.parent.resolve() / "LLMs" / "auxiliary_llm" / "model_weight"
    _fallback_gguf_path = _aux_model_weight / _GGUF_FILENAME

    # ------------------------------------------------------------------
    # 2a.  Helper: resolve model file path (download from HF if missing)
    # ------------------------------------------------------------------

    def _resolve_model_path() -> str:
        """Return the local GGUF path, downloading from Hugging Face if needed.
        
        Checks in order:
          1. ITTT_model/model_weight/
          2. auxiliary_llm/model_weight/ (copy to ITTT dir if found)
          3. Download from Hugging Face
        """
        if _gguf_path.is_file():
            return str(_gguf_path)

        # Check auxiliary_llm fallback location
        if _fallback_gguf_path.is_file():
            print(f"Copying GGUF from {_fallback_gguf_path} -> {_gguf_path} ...")
            import shutil
            _MODEL_WEIGHT_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(_fallback_gguf_path), str(_gguf_path))
            return str(_gguf_path)

        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise ImportError(
                "Model file not found locally and 'huggingface_hub' is not installed. "
                "Run: pip install huggingface_hub"
            ) from None

        print(f"Downloading {_HF_REPO_ID}/{_GGUF_FILENAME} -> {_MODEL_WEIGHT_DIR} ...")
        hf_hub_download(
            repo_id=_HF_REPO_ID,
            filename=_GGUF_FILENAME,
            local_dir=str(_MODEL_WEIGHT_DIR),
        )
        return str(_gguf_path)

    # ------------------------------------------------------------------
    # 2b.  Helper: resolve mmproj path (download if missing)
    # ------------------------------------------------------------------

    def _resolve_mmproj_path() -> str:
        """Return the local mmproj path, downloading from HF if needed."""
        if _mmproj_path.is_file():
            return str(_mmproj_path)

        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise ImportError(
                "mmproj file not found locally and 'huggingface_hub' is not installed. "
                "Run: pip install huggingface_hub"
            ) from None

        print(f"Downloading {_HF_REPO_ID}/{_MMPROJ_FILENAME} -> {_MODEL_WEIGHT_DIR} ...")
        hf_hub_download(
            repo_id=_HF_REPO_ID,
            filename=_MMPROJ_FILENAME,
            local_dir=str(_MODEL_WEIGHT_DIR),
        )
        return str(_mmproj_path)

    # ------------------------------------------------------------------
    # 2c.  Message converter (supports multimodal HumanMessage)
    # ------------------------------------------------------------------

    def _convert_message_to_dict(message: BaseMessage) -> Dict[str, Any]:
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

    class LocalLlamaChatModel(BaseChatModel):
        """LangChain ``BaseChatModel`` wrapping ``llama_cpp.Llama`` with
        ``Qwen25VLChatHandler`` for local GGUF multimodal inference."""

        model_path: str = ""
        mmproj_path: str = ""
        n_ctx: int = 8192
        temperature: float = 0.0
        max_tokens: int = 4096
        verbose: bool = False
        n_gpu_layers: int = -1  # -1 = offload all layers to GPU

        _client: Optional[Llama] = None
        _resolved_model_path: str = ""
        _resolved_mmproj_path: str = ""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._resolved_model_path = self.model_path or _resolve_model_path()
            self._resolved_mmproj_path = self.mmproj_path or _resolve_mmproj_path()

        def _ensure_client(self) -> Llama:
            if self._client is None:
                chat_handler = Qwen25VLChatHandler(
                    clip_model_path=self._resolved_mmproj_path,
                    verbose=self.verbose,
                )
                self._client = Llama(
                    model_path=self._resolved_model_path,
                    chat_handler=chat_handler,
                    n_ctx=self.n_ctx,
                    n_batch=self.n_ctx,
                    n_ubatch=self.n_ctx,  # Must match or exceed n_tokens per encoder step
                    n_gpu_layers=self.n_gpu_layers,
                    verbose=self.verbose,
                )
                atexit.register(self._release_client)
            return self._client

        def _release_client(self) -> None:
            if self._client is not None:
                self._client.close()
                self._client = None

        @property
        def _llm_type(self) -> str:
            return "local-llama-cpp-multimodal"

        @property
        def _identifying_params(self) -> Mapping[str, Any]:
            return {
                "model_path": self.model_path,
                "mmproj_path": self.mmproj_path,
                "n_ctx": self.n_ctx,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }

        def _generate(
            self,
            messages: List[BaseMessage],
            stop: Optional[List[str]] = None,
            run_manager: Optional[CallbackManagerForLLMRun] = None,
            **kwargs: Any,
        ) -> ChatResult:
            client = self._ensure_client()
            try:
                llama_messages = [_convert_message_to_dict(m) for m in messages]
                response = client.create_chat_completion(
                    messages=llama_messages,
                    stop=stop or [],
                    temperature=kwargs.get("temperature", self.temperature),
                    max_tokens=kwargs.get("max_tokens", self.max_tokens),
                )
                choice = response["choices"][0]
                message = choice["message"]
                content = message.get("content", "")
            finally:
                self._release_client()
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        @property
        def lc_attributes(self) -> Mapping[str, Any]:
            return self._identifying_params

    # ------------------------------------------------------------------
    # 2e.  Instantiate the singleton
    # ------------------------------------------------------------------

    ITTT_model = LocalLlamaChatModel().configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )