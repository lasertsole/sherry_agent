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

import json
import os
import instructor
import uuid
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import ConfigurableField, Runnable, RunnableLambda
from langchain_core.outputs import ChatGeneration, ChatResult

from config import ENV_PATH


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
        _max_tokens = min(int(_raw_max), 65536) if _raw_max else 65536

        _model_config: dict[str, Any] = {
            "model_provider": _provider,
            "model": _api_name,
            "api_key": _api_key,
            "base_url": _api_base,
            "temperature": temperature if temperature is not None else 0,
            "max_retries": 2,
            "profile": {"max_input_tokens": _max_tokens},
        }
        _model_config = {k: v for k, v in _model_config.items() if v is not None and v != ""}

        model = init_chat_model(**_model_config).configurable_fields(
            temperature=ConfigurableField(id="temperature"),
        )

    else:
        # ---------- Local (GGUF) branch ----------
        import atexit
        from llama_cpp import Llama

        model_weight_dir = Path(__file__).parent.resolve() / "model_weight"
        _model_path = model_weight_dir / "Qwen3.5-9B-Q4_K_M.gguf"
        _HF_REPO_ID = "lmstudio-community/Qwen3.5-9B-GGUF"
        _HF_FILENAME = "Qwen3.5-9B-Q4_K_M.gguf"

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
            # ── hf_hub_download(local_files_only=True) does NOT work with
            #    local_dir — it only looks in HF's own cache (~/.cache/huggingface/hub).
            #    So we skip it entirely and go straight to remote download. ──
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

        _local_temperature = temperature if temperature is not None else 0.0

        class LocalLlamaChatModel(BaseChatModel):
            """LangChain BaseChatModel wrapping llama_cpp.Llama for local GGUF models."""

            model_path: str = ""
            n_ctx: int = 40960
            temperature: float = _local_temperature
            max_tokens: int = 32768
            verbose: bool = False
            n_gpu_layers: int = -1  # -1 = offload all layers to GPU

            _client: Optional[Llama] = None
            _resolved_path: str = ""

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._resolved_path = self.model_path or _resolve_model_path()

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

            def _release_client(self) -> None:
                if self._client is not None:
                    self._client.close()
                    self._client = None

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

            def bind_tools(
                self,
                tools: Sequence[Union[Dict[str, Any], type, Any]],
                *,
                tool_choice: Optional[str] = None,
                **kwargs: Any,
            ) -> Runnable:
                """Bind tools by injecting a tool-calling system prompt.

                For local GGUF models that lack native tool-call support, this
                instructs the model to respond with a JSON object containing the
                tool name and arguments, then wraps ``_generate`` to parse the
                tool call into the standard ``AIMessage.tool_calls`` format.
                """
                # ── Build tool schemas into a descriptive prompt ────────────
                tool_descriptions = []
                for t in tools:
                    if isinstance(t, dict):
                        name = t.get("name") or t.get("function", {}).get("name", "unknown")
                        desc = t.get("description") or t.get("function", {}).get("description", "")
                        params = t.get("parameters") or t.get("function", {}).get("parameters", {})
                    elif hasattr(t, "model_fields"):
                        # Pydantic model
                        name = getattr(t, "__name__", str(t))
                        desc = getattr(t, "__doc__", "")
                        params = {}
                        for fname, field in t.model_fields.items():
                            params[fname] = {
                                "type": str(field.annotation.__name__ if hasattr(field.annotation, '__name__') else field.annotation),
                                "description": (field.description or ""),
                            }
                    elif hasattr(t, "name"):
                        # BaseTool / @tool-decorated function
                        name = t.name
                        desc = getattr(t, "description", "")
                        args_schema = getattr(t, "args_schema", None)
                        if args_schema and hasattr(args_schema, "model_fields"):
                            params = {}
                            for fname, field in args_schema.model_fields.items():
                                params[fname] = {
                                    "type": str(field.annotation.__name__ if hasattr(field.annotation, '__name__') else field.annotation),
                                    "description": (field.description or ""),
                                }
                        else:
                            params = getattr(t, "args", {})
                    else:
                        continue
                    tool_descriptions.append({
                        "name": name,
                        "description": desc,
                        "parameters": params,
                    })

                tool_prompt = (
                    "You have access to the following tools. When you need to use a tool, "
                    "respond with ONLY a valid JSON object in this exact format:\n"
                    '{"name": "<tool_name>", "arguments": {<tool_args>}}\n\n'
                    "Do NOT include any other text before or after the JSON object.\n\n"
                    "Available tools:\n"
                )
                for td in tool_descriptions:
                    tool_prompt += f"\n### {td['name']}\n{td['description']}\n"
                    if td['parameters']:
                        tool_prompt += f"Parameters: {json.dumps(td['parameters'], ensure_ascii=False, default=str)}\n"

                if tool_choice and tool_choice != "any":
                    tool_prompt += f"\nYou MUST use the tool '{tool_choice}'. Do not use any other tool.\n"

                def _invoke_with_tools(input_data: Any) -> AIMessage:
                    if isinstance(input_data, str):
                        msgs: List[BaseMessage] = [
                            HumanMessage(content=input_data),
                        ]
                    elif isinstance(input_data, list):
                        msgs = list(input_data)
                    else:
                        msgs = [HumanMessage(content=str(input_data))]

                    # Inject tool instructions as a system message (prepend)
                    has_system = any(isinstance(m, SystemMessage) for m in msgs)
                    if has_system:
                        for i, m in enumerate(msgs):
                            if isinstance(m, SystemMessage):
                                msgs[i] = SystemMessage(
                                    content=m.content + "\n\n" + tool_prompt if m.content else tool_prompt
                                )
                                break
                    else:
                        msgs.insert(0, SystemMessage(content=tool_prompt))

                    result = self._generate(msgs)
                    content = result.generations[0].message.content or ""

                    # ── Parse tool call from response ──
                    parsed_tool_calls = []
                    cleaned = content.strip()
                    # Try to extract JSON from the response (handle code fences)
                    if cleaned.startswith("```"):
                        for line in cleaned.split("\n"):
                            if line.strip().startswith("{"):
                                cleaned = line.strip()
                                break
                        else:
                            cleaned = cleaned.strip("`").strip()

                    if cleaned.startswith("{"):
                        try:
                            obj = json.loads(cleaned)
                            name = obj.get("name", "")
                            args = obj.get("arguments", obj.get("args", {}))
                            if name:
                                parsed_tool_calls.append({
                                    "name": name,
                                    "args": args if isinstance(args, dict) else {},
                                    "id": f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "tool_call",
                                })
                        except json.JSONDecodeError:
                            pass

                    return AIMessage(
                        content=content,
                        tool_calls=parsed_tool_calls if parsed_tool_calls else None,
                    )

                return RunnableLambda(_invoke_with_tools)

            def with_structured_output(
                self,
                schema: Union[type, Dict[str, Any]],
                *,
                include_raw: bool = False,
                **kwargs: Any,
            ) -> Runnable:
                """Implement structured output via prompt-based JSON generation.

                For local GGUF models that don't support native tool calling,
                this injects a JSON format instruction into the system prompt
                and parses the response with ``PydanticOutputParser``.
                """
                _ = kwargs.pop("method", None)
                _ = kwargs.pop("strict", None)
                if kwargs:
                    msg = f"Received unsupported arguments {kwargs}"
                    raise ValueError(msg)

                def _invoke_with_structured(
                    input_data: Any,
                ) -> Any:
                    if isinstance(input_data, str):
                        msgs: List[BaseMessage] = [
                            HumanMessage(content=input_data),
                        ]
                    elif isinstance(input_data, list):
                        msgs = list(input_data)
                    else:
                        msgs = [HumanMessage(content=str(input_data))]

                    try:
                        client = self._ensure_client()
                        create = instructor.patch(
                            create=client.create_chat_completion_openai_v1,
                            mode=instructor.Mode.JSON,
                        )

                        return create(
                            messages=[_convert_message_to_dict(m) for m in msgs],
                            response_model=schema,
                        )
                    finally:
                        self._release_client()

                return RunnableLambda(_invoke_with_structured)

            @property
            def lc_attributes(self) -> Mapping[str, Any]:
                return self._identifying_params

        model = LocalLlamaChatModel().configurable_fields(
            temperature=ConfigurableField(id="temperature"),
        )

    return model
