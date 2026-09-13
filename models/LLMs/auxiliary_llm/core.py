"""Auxiliary LLM — auto-selects between remote API and local GGUF.

If ``AUXILIARY_LLM_MODEL_LOCAL=true`` is set in ``.env``, uses the local
GGUF model (``Qwen3.5-9B-Q4_K_M.gguf``).

Otherwise, when ``AUXILIARY_LLM_PROVIDER`` or ``AUXILIARY_LLM_API_BASE``
is configured, uses the remote API via ``init_chat_model()``.
The local branch serves as the ultimate fallback when neither a provider
nor a local switch is set.

Usage:
    from models.LLMs import auxiliary_llm
    result = auxiliary_llm.invoke("Hello")
    structured = auxiliary_llm.with_structured_output(SomeModel).invoke("...")

    # In subagent threads, create a fresh instance bound to the current event loop:
    from models.LLMs.auxiliary_llm.core import build_auxiliary_llm
    fresh = build_auxiliary_llm()
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Any
from collections.abc import Sequence
from langchain_core.runnables import Runnable

from config import ENV_PATH
from config.features import LLM_CLIENT_DEFAULTS
from models.LLMs.base_local_llama import LocalLlamaChatBase
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.LLMs.auxiliary_llm.local_adapters import LocalStructuredOutput, LocalToolBinder


# ---------------------------------------------------------------------------
# 1.  Read and prepare environment
# ---------------------------------------------------------------------------

load_dotenv(ENV_PATH, override=True)


# ---------------------------------------------------------------------------
# 2.  Factory function (avoids event-loop binding at import time)
# ---------------------------------------------------------------------------


def build_auxiliary_llm(temperature: float | None = None):
    """Create a fresh auxiliary LLM instance bound to the current event loop.

    The module-level ``auxiliary_llm`` singleton is created at import time on
    the main thread.  Its internal ``openai.AsyncOpenAI`` → ``httpx.AsyncClient``
    transport pool contains ``asyncio.Lock`` objects that are bound to the
    event loop active at creation time.  When the subagent daemon thread
    tries to use this same client via ``agent.ainvoke()``, those locks
    deadlock silently.

    Call this factory from any async context (e.g. the subagent daemon
    thread) to get a fresh instance whose transport pool is correctly
    bound to the *current* event loop.
    """
    _provider = os.getenv("AUXILIARY_LLM_PROVIDER", "").strip()
    _api_key = os.getenv("AUXILIARY_LLM_API_KEY", "").strip()
    _api_base = os.getenv("AUXILIARY_LLM_API_BASE", "").strip()

    _is_local = os.getenv("AUXILIARY_LLM_MODEL_LOCAL", "").strip().lower() == "true"

    if not _is_local and (_provider or _api_base):
        # ---------- Remote (online) branch ----------
        from langchain.chat_models import init_chat_model

        _api_name = os.getenv("AUXILIARY_LLM_API_NAME", "").strip()
        _raw_max = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
        _max_tokens = int(_raw_max) if _raw_max else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

        _model_config: dict[str, Any] = {
            "model_provider": _provider,
            "model": _api_name,
            "api_key": _api_key,
            "base_url": _api_base,
            "temperature": temperature if temperature is not None else 0,
            "max_retries": LLM_CLIENT_DEFAULTS["aux_max_retries"],
            "profile": {"max_input_tokens": _max_tokens},
        }
        _model_config = {k: v for k, v in _model_config.items() if v is not None and v != ""}

        model = init_chat_model(**_model_config)
        model = NormalizingChatModel(inner=model)

    else:
        # ---------- Local (GGUF) branch ----------
        import atexit
        from llama_cpp import Llama

        model_weight_dir = Path(__file__).parent.resolve() / "model_weight"
        _model_path = model_weight_dir / "Qwen3.5-9B-Q4_K_M.gguf"
        _HF_REPO_ID = "lmstudio-community/Qwen3.5-9B-GGUF"  # noqa: N806
        _HF_FILENAME = "Qwen3.5-9B-Q4_K_M.gguf"  # noqa: N806

        def _resolve_model_path() -> str:
            # Shared resolver (audit 1.1.3); hf_hub_download(local_files_only=True)
            # does NOT work with local_dir — go straight to remote download.
            from models.utils import resolve_gguf_path

            return resolve_gguf_path(_model_path, _HF_REPO_ID, _HF_FILENAME, model_weight_dir)

        _local_temperature = temperature if temperature is not None else 0.0

        class LocalLlamaChatModel(LocalLlamaChatBase):
            """Auxiliary variant: plain Llama client + reasoning extraction.

            Common lifecycle/fields live on LocalLlamaChatBase (audit 1.1.1);
            tool binding and structured output are delegated to the
            prompt-injection adapters in local_adapters.py.
            """

            extract_reasoning: bool = True

            def _resolve_model_path(self) -> str:
                return _resolve_model_path()

            def _ensure_client(self) -> Llama:
                if self._client is None:
                    self._client = Llama(
                        model_path=self._resolved_path,
                        n_ctx=self.n_ctx,
                        n_gpu_layers=self.n_gpu_layers,
                        verbose=self.verbose,
                    )
                    atexit.register(self._release_client)
                return self._client

            @property
            def _llm_type(self) -> str:
                return "local-llama-cpp"

            def bind_tools(
                self,
                tools: Sequence[dict[str, Any] | type | Any],
                *,
                tool_choice: str | None = None,
                **kwargs: Any,
            ) -> Runnable:
                return LocalToolBinder(self).bind_tools(tools, tool_choice=tool_choice, **kwargs)

            def with_structured_output(
                self,
                schema: type | dict[str, Any],
                *,
                include_raw: bool = False,
                **kwargs: Any,
            ) -> Runnable:
                return LocalStructuredOutput(self).with_structured_output(
                    schema, include_raw=include_raw, **kwargs
                )

        model = LocalLlamaChatModel(n_ctx=40960, temperature=_local_temperature, max_tokens=32768)
        model = NormalizingChatModel(inner=model)

    return model
