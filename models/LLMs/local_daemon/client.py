"""Client shim for the auxiliary chat-model daemon.

The local chat path calls two raw llama-cpp methods:

* ``create_chat_completion(**kwargs)`` → the llama-cpp completion dict;
* ``create_chat_completion_openai_v1(**kwargs)`` → an OpenAI ``ChatCompletion``
  (instructor patches this for structured output).

:class:`DaemonChatClient` implements exactly those two over loopback HTTP. The
daemon owns the weights, the client only serialises the call. When the daemon
cannot be started, :meth:`DaemonChatClient.available` answers False and the
caller keeps its in-process ``Llama`` path.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

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

_DAEMON = DaemonSpec(
    name="local chat",
    host=LOCAL_DAEMONS["host"],
    port=LOCAL_DAEMONS["auxiliary_port"],
    pid_path=APP_HOME / ".llm_daemon.pid",
    import_fn="models.LLMs.local_daemon.server",
    ready_wait_s=LOCAL_DAEMONS["auxiliary_ready_wait_s"],
    liveness_timeout_s=LOCAL_DAEMONS["liveness_timeout_s"],
)
_DAEMON_LOG = LOG_DIR / "daemons" / "local_chat.log"

#: Params forwarded from ``create_chat_completion_openai_v1`` (the ones the
#: local structured-output path can produce); instructor's own control kwargs
#: (``response_model``, ``mode`` …) stay on this side of the wire.
_OPENAI_FORWARD_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "response_format",
)

#: Params accepted by the plain llama-cpp call shape.
_CHAT_FORWARD_KEYS = _OPENAI_FORWARD_KEYS + (
    "top_k",
    "repeat_penalty",
    "logprobs",
    "echo",
    "tools",
    "tool_choice",
)


class DaemonChatClient:
    """A llama-cpp-compatible client backed by the local chat daemon."""

    def __init__(
        self,
        *,
        model_path: str,
        n_ctx: int,
        n_gpu_layers: int = -1,
        verbose: bool = False,
    ) -> None:
        self._model_path = model_path
        self._n_ctx = int(n_ctx)
        self._n_gpu_layers = int(n_gpu_layers)
        self._verbose = bool(verbose)
        self._ready = False

    # ---- lifecycle ---------------------------------------------------

    def _ensure(self) -> None:
        if self._ready:
            return
        state = ensure_ready(
            _DAEMON,
            extra_env={
                "SHERRY_LOCAL_LLM_MODEL_PATH": self._model_path,
                "SHERRY_LOCAL_LLM_N_CTX": str(self._n_ctx),
                "SHERRY_LOCAL_LLM_N_GPU_LAYERS": str(self._n_gpu_layers),
                "SHERRY_LOCAL_LLM_VERBOSE": "1" if self._verbose else "0",
            },
            log_path=_DAEMON_LOG,
        )
        if state is not ReadyState.READY:
            raise DaemonError(f"local chat daemon is {state.value}")
        self._ready = True

    def available(self) -> bool:
        """Ensure the daemon is ready; False when the caller should fall back."""
        try:
            self._ensure()
        except Exception as error:  # noqa: BLE001 - fallback is the contract here
            logger.warning("local chat daemon unavailable: {}", error)
            return False
        return True

    def close(self) -> None:
        """No-op: the daemon owns the weights and its idle timer."""

    # ---- transport ---------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        try:
            body = request_json(
                _DAEMON,
                "/v1/chat/completions",
                payload,
                timeout_s=LOCAL_DAEMONS["auxiliary_http_timeout_s"],
            )
        except DaemonError:
            # A daemon that idled out between calls is retried once.
            self._ready = False
            self._ensure()
            body = request_json(
                _DAEMON,
                "/v1/chat/completions",
                payload,
                timeout_s=LOCAL_DAEMONS["auxiliary_http_timeout_s"],
            )
        if not isinstance(body, dict) or not body.get("choices"):
            raise DaemonError("local chat daemon returned a malformed completion")
        return body

    @staticmethod
    def _forward(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        body = {"messages": payload.get("messages", [])}
        for key in keys:
            if key in payload:
                body[key] = payload[key]
        return body

    # ---- llama-cpp surface -------------------------------------------

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        return self._post(self._forward(kwargs, _CHAT_FORWARD_KEYS))

    def create_chat_completion_openai_v1(self, *args: Any, **kwargs: Any) -> Any:
        from openai.types.chat import ChatCompletion

        raw = dict(self._post(self._forward(kwargs, _OPENAI_FORWARD_KEYS)))
        raw.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
        raw.setdefault("object", "chat.completion")
        raw.setdefault("created", int(time.time()))
        raw.setdefault("model", "local-llama")
        return ChatCompletion.model_validate(raw)


def build_daemon_chat_client(
    *, model_path: str, n_ctx: int, n_gpu_layers: int = -1, verbose: bool = False
) -> DaemonChatClient:
    return DaemonChatClient(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=verbose,
    )


__all__ = ["DaemonChatClient", "build_daemon_chat_client"]
