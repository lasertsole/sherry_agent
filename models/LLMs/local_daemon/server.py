"""Auxiliary chat-model daemon: one warm GGUF behind loopback HTTP.

The local auxiliary path is non-streaming and prompt-driven: ``LocalToolBinder``
injects a JSON instruction and ``LocalStructuredOutput`` patches
``create_chat_completion_openai_v1`` with instructor. Both call the llama-cpp
client directly, so the daemon exposes the same shape rather than an
OpenAI-compatible server with a different contract:

    GET  /healthy              → {"ready": bool}
    POST /v1/chat/completions  → llama-cpp ``create_chat_completion`` kwargs
                                 → the raw completion dict

The model is loaded from the environment the shared lifecycle injects
(``SHERRY_LOCAL_LLM_MODEL_PATH`` etc.), so the daemon holds no configuration of
its own beyond the port and idle timeout.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from loguru import logger

from config.features import LOCAL_DAEMONS
from models.daemon.base import IdleWatchdog, send_json

HOST: str = LOCAL_DAEMONS["host"]
# The child is one ROLE of the shared server (text chat on 9014, vision on
# 9015); the client passes its spec's port, falling back to the auxiliary one.
try:
    PORT: int = int(os.environ.get("SHERRY_LOCAL_LLM_DAEMON_PORT", ""))
except ValueError:
    PORT = int(LOCAL_DAEMONS["auxiliary_port"])
_HEALTHY_PATH = "/healthy"
_CHAT_PATH = "/v1/chat/completions"
_MAX_BODY_BYTES = 64 * 1024 * 1024
#: Params the local chat path actually sends (``_generate``, instructor JSON
#: mode, the multimodal-free adapters). Anything else is refused instead of
#: being forwarded into llama-cpp's very permissive kwargs.
_ALLOWED_PARAMS = frozenset(
    {
        "messages",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "stop",
        "seed",
        "response_format",
        "frequency_penalty",
        "presence_penalty",
        "repeat_penalty",
        "logprobs",
        "echo",
        "tools",
        "tool_choice",
        # Accepted only to reject it explicitly: the local path never streams,
        # and a caller that asked for a stream would otherwise parse a plain
        # completion as an iterator.
        "stream",
    }
)

_model: Any | None = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()


def _model_path() -> str:
    path = os.environ.get("SHERRY_LOCAL_LLM_MODEL_PATH", "").strip()
    if not path:
        raise RuntimeError("SHERRY_LOCAL_LLM_MODEL_PATH is not set")
    return path


def _mmproj_path() -> str:
    return os.environ.get("SHERRY_LOCAL_LLM_MMPROJ_PATH", "").strip()


def _n_ctx() -> int:
    raw = os.environ.get("SHERRY_LOCAL_LLM_N_CTX", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return int(LOCAL_DAEMONS["auxiliary_n_ctx"])
    return value if value > 0 else int(LOCAL_DAEMONS["auxiliary_n_ctx"])


def _n_gpu_layers() -> int:
    raw = os.environ.get("SHERRY_LOCAL_LLM_N_GPU_LAYERS", "").strip()
    try:
        return int(raw)
    except ValueError:
        return -1


def _get_model() -> Any:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from llama_cpp import Llama

            mmproj = _mmproj_path()
            logger.info(
                "Local chat daemon loading {}{} …",
                _model_path(),
                " (vision mmproj)" if mmproj else "",
            )
            kwargs: dict[str, Any] = {
                "model_path": _model_path(),
                "n_ctx": _n_ctx(),
                "n_gpu_layers": _n_gpu_layers(),
                "verbose": False,
            }
            if mmproj:
                # The multimodal in-process path uses Qwen25VLChatHandler and
                # sizes both batches to the context; mirror it exactly here.
                from llama_cpp.llama_chat_format import Qwen25VLChatHandler

                kwargs["chat_handler"] = Qwen25VLChatHandler(clip_model_path=mmproj, verbose=False)
                kwargs["n_batch"] = kwargs["n_ctx"]
                kwargs["n_ubatch"] = kwargs["n_ctx"]
            _model = Llama(**kwargs)
            logger.info("Local chat daemon model ready.")
    return _model


def _parse_request(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - _ALLOWED_PARAMS)
    if unknown:
        raise ValueError(f"unsupported parameter(s): {', '.join(unknown)}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if not all(isinstance(message, dict) for message in messages):
        raise ValueError("each message must be an object")
    if payload.get("stream") is True:
        raise ValueError("streaming is not supported by the local chat daemon")
    return payload


class _Handler(BaseHTTPRequestHandler):
    watchdog: IdleWatchdog

    def log_message(self, format: str, *args: tuple[object, ...]) -> None:  # noqa: A002
        logger.debug(f"local chat daemon: {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        self.watchdog.touch()
        if self.path.rsplit("?", 1)[0].rstrip("/") != _HEALTHY_PATH:
            self.send_error(404)
            return
        ready = _model is not None
        send_json(self, 200, {"status": "ok" if ready else "warming", "ready": ready})

    def do_POST(self) -> None:  # noqa: N802
        self.watchdog.touch()
        if self.path.rsplit("?", 1)[0].rstrip("/") != _CHAT_PATH:
            send_json(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > _MAX_BODY_BYTES:
                send_json(self, 413, {"error": "request body is empty or too large"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            request = _parse_request(payload)
            model = _get_model()
            with _infer_lock:
                result = model.create_chat_completion(**request)
            send_json(self, 200, result)
        except Exception as error:  # noqa: BLE001 - one bad call must not kill the daemon
            logger.exception(f"local chat daemon error: {error}")
            send_json(self, 500, {"error": str(error)[:400]})


def _idle_timeout_s() -> float:
    try:
        return float(
            os.environ.get(
                "SHERRY_LOCAL_LLM_DAEMON_IDLE_TIMEOUT_S",
                str(LOCAL_DAEMONS["auxiliary_idle_timeout_s"]),
            )
        )
    except ValueError:
        return float(LOCAL_DAEMONS["auxiliary_idle_timeout_s"])


def main() -> None:
    threading.Thread(target=_get_model, daemon=True).start()
    watchdog = IdleWatchdog(
        _idle_timeout_s(),
        lambda: os._exit(0),
    )
    _Handler.watchdog = watchdog
    watchdog.start()
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    logger.info(f"Local chat daemon listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
