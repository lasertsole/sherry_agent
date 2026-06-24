"""Auxiliary LLM — auto-selects between remote API and local GGUF.

If ``AUXILIARY_LLM_PROVIDER`` or ``AUXILIARY_LLM_API_BASE`` is set in ``.env``,
uses the remote API via ``init_chat_model()``.
Otherwise falls back to the local GGUF model (``Qwen3-8B-Q4_K_M.gguf``).

Usage:
    from models.LLMs import auxiliary_llm
    result = auxiliary_llm.invoke("Hello")
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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
from langchain_core.language_models import LanguageModelInput

# ---------------------------------------------------------------------------
# 1.  Read & prepare environment
# ---------------------------------------------------------------------------

# Locate .env from project root (same logic as config.path)
_env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(_env_path, override=True)

_provider = os.getenv("AUXILIARY_LLM_PROVIDER", "").strip()
_api_key = os.getenv("AUXILIARY_LLM_API_KEY", "").strip()
_api_base = os.getenv("AUXILIARY_LLM_API_BASE", "").strip()

# ---------------------------------------------------------------------------
# 2.  Decide remote vs. local
#    Remote when a provider is configured (Ollama / OpenAI / etc.)
#    or an API base is explicitly set.
# ---------------------------------------------------------------------------

if _provider or _api_base:
    # ---------- Remote (online) branch ----------
    from langchain.chat_models import init_chat_model

    _api_name = os.getenv("AUXILIARY_LLM_API_NAME", "").strip()
    _raw_max = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
    _max_tokens = min(int(_raw_max), 65536) if _raw_max else 65536

    _model_config: dict[str, Any] = {
        "model_provider": _provider,
        "model": _api_name,
        "api_key": _api_key,
        "base_url": _api_base,
        "temperature": 0,
        "max_retries": 2,
        "profile": {"max_input_tokens": _max_tokens},
    }
    _model_config = {k: v for k, v in _model_config.items() if v is not None and v != ""}

    auxiliary_llm = init_chat_model(**_model_config).configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )

    # Export alias for type-checking / isinstance
    def _is_remote() -> bool:
        return True

else:
    # ---------- Local (GGUF) branch ----------
    import atexit
    from llama_cpp import Llama

    model_weight_dir = Path(__file__).parent.resolve() / "model_weight"
    _model_path = model_weight_dir / "Qwen3-8B-Q4_K_M.gguf"
    _HF_REPO_ID = "Qwen/Qwen3-8B-GGUF"
    _HF_FILENAME = "Qwen3-8B-Q4_K_M.gguf"

    def _resolve_model_path() -> str:
        if _model_path.is_file():
            return str(_model_path)
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise ImportError(
                "Model file not found locally and 'huggingface_hub' is not installed. "
                "Run: pip install huggingface_hub"
            ) from None
        print(f"Downloading {_HF_REPO_ID}/{_HF_FILENAME} -> {model_weight_dir} ...")
        hf_hub_download(
            repo_id=_HF_REPO_ID,
            filename=_HF_FILENAME,
            local_dir=str(model_weight_dir),
        )
        return str(_model_path)

    def _convert_message_to_dict(message: BaseMessage) -> Dict[str, Any]:
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": message.content}
        elif isinstance(message, AIMessage):
            return {"role": "assistant", "content": message.content}
        elif isinstance(message, SystemMessage):
            return {"role": "system", "content": message.content}
        else:
            return {"role": "user", "content": message.content}

    class LocalLlamaChatModel(BaseChatModel):
        """LangChain BaseChatModel wrapping llama_cpp.Llama for local GGUF models."""

        model_path: str = ""
        n_ctx: int = 40960
        temperature: float = 0.0
        max_tokens: int = 32768
        verbose: bool = False

        _client: Llama = None  # type: ignore[assignment]

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            resolved = self.model_path or _resolve_model_path()
            self._client = Llama(
                model_path=resolved,
                n_ctx=self.n_ctx,
                verbose=self.verbose,
            )
            atexit.register(self._client.close)

        @property
        def _llm_type(self) -> str:
            return "local-llama-cpp"

        @property
        def _identifying_params(self) -> Mapping[str, Any]:
            return {
                "model_path": self.model_path,
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
            llama_messages = [_convert_message_to_dict(m) for m in messages]
            response = self._client.create_chat_completion(
                messages=llama_messages,
                stop=stop or [],
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
            )
            choice = response["choices"][0]
            message = choice["message"]
            content = message.get("content", "")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        @property
        def lc_attributes(self) -> Dict[str, Any]:
            return self._identifying_params

    auxiliary_llm = LocalLlamaChatModel().configurable_fields(
        temperature=ConfigurableField(id="temperature"),
    )

    def _is_remote() -> bool:
        return False