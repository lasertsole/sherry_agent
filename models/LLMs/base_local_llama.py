"""Template Method base for local GGUF chat models (audit 1.1.1-1.1.3).

``LocalLlamaChatBase`` owns the common pydantic fields, client lifecycle,
``_generate`` skeleton and identifying params; ``LocalMultimodalLlamaChatBase``
adds the mmproj field and the Qwen25VLChatHandler client shared by the vision
models. Per-model subclasses only implement the hooks:

* ``_resolve_model_path()`` / ``_resolve_mmproj_path()`` — weight resolution
* ``_ensure_client()`` — backend construction (plain Llama vs chat-handler)
* ``_convert_message_to_dict(msg)`` — message-shape variant
* ``_llm_type`` — model family tag

The three duplicated ``LocalLlamaChatModel`` classes (auxiliary_llm /
ITTT_model / VTTT_model) shrink to hook implementations on top of these bases.
"""

import atexit
from typing import Any, Dict, List, Mapping, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class LocalLlamaChatBase(BaseChatModel):
    """Common base: fields, client lifecycle, generate skeleton."""

    model_path: str = ""
    n_ctx: int = 4096
    temperature: float = 0.0
    max_tokens: int = 4096
    verbose: bool = False
    n_gpu_layers: int = -1
    # auxiliary extracts reasoning_content/reasoning; vision models ignore it
    extract_reasoning: bool = False

    _client: Optional[Any] = None
    _resolved_path: str = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._resolved_path = self.model_path or self._resolve_model_path()

    # ---- hooks -----------------------------------------------------------

    def _resolve_model_path(self) -> str:
        raise NotImplementedError

    def _ensure_client(self) -> Any:
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        raise NotImplementedError

    def _convert_message_to_dict(self, message: BaseMessage) -> Dict[str, Any]:
        """Default text-only conversion (auxiliary variant)."""
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": message.content}
        if isinstance(message, AIMessage):
            return {"role": "assistant", "content": message.content}
        # SystemMessage and everything else
        return {"role": "user" if not isinstance(message, SystemMessage) else "system", "content": message.content}

    # ---- shared lifecycle --------------------------------------------------

    def _release_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {
            "model_path": self.model_path,
            "n_ctx": self.n_ctx,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @property
    def lc_attributes(self) -> Mapping[str, Any]:
        return self._identifying_params

    # ---- generate skeleton -------------------------------------------------

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        client = self._ensure_client()
        try:
            llama_messages = [self._convert_message_to_dict(m) for m in messages]
            response = client.create_chat_completion(
                messages=llama_messages,
                stop=stop or [],
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
            )
            choice = response["choices"][0]
            message = choice["message"]
            content = message.get("content", "")
            reason: Optional[str] = None
            if self.extract_reasoning:
                reason = message.get("reasoning_content")
                if reason is None:
                    reason = message.get("reasoning")
        finally:
            self._release_client()
        ai_kwargs: dict[str, Any] = {}
        if isinstance(reason, str) and reason:
            ai_kwargs.setdefault("reasoning_content", reason)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, additional_kwargs=ai_kwargs))]
        )


class LocalMultimodalLlamaChatBase(LocalLlamaChatBase):
    """Adds mmproj + Qwen25VLChatHandler client (shared by ITTT/VTTT)."""

    mmproj_path: str = ""
    _resolved_mmproj_path: str = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._resolved_mmproj_path = self.mmproj_path or self._resolve_mmproj_path()

    def _resolve_mmproj_path(self) -> str:
        raise NotImplementedError

    def _ensure_client(self) -> Any:
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import Qwen25VLChatHandler

        if self._client is None:
            chat_handler = Qwen25VLChatHandler(
                clip_model_path=self._resolved_mmproj_path,
                verbose=self.verbose,
            )
            self._client = Llama(
                model_path=self._resolved_path,
                chat_handler=chat_handler,
                n_ctx=self.n_ctx,
                n_batch=self.n_ctx,
                n_ubatch=self.n_ctx,  # Must match or exceed n_tokens per encoder step
                n_gpu_layers=self.n_gpu_layers,
                verbose=self.verbose,
            )
            atexit.register(self._release_client)
        return self._client

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {
            "model_path": self.model_path,
            "mmproj_path": self.mmproj_path,
            "n_ctx": self.n_ctx,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
