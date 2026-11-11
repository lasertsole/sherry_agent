"""Embedding daemon: one warm bge-m3 GGUF behind loopback HTTP.

The main process used to ``Llama(...)`` / ``close()`` a 606 MB GGUF on every
embed call — a ~1.3 s hot load and a ~1 GB RSS peak per call. This daemon keeps
one context alive in a child process and serialises inference (llama-cpp is not
thread-safe), so the orchestrator only ever holds a few JSON vectors.

Endpoints (JSON, loopback only):
    GET  /healthy    → {"ready": bool} — true once the GGUF is loaded
    POST /embeddings → {"input": ["…", …]} → {"data": [{"index", "embedding"}, …]}

Run: ``python -c "from models.embed_model.text.daemon import main; main()"``
(the shared lifecycle in ``models/daemon/base.py`` starts and stops it).
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
PORT: int = LOCAL_DAEMONS["embed_port"]
_HEALTHY_PATH = "/healthy"
_EMBED_PATH = "/embeddings"
# A request body larger than this is a client bug, not a legitimate batch.
_MAX_BODY_BYTES = 32 * 1024 * 1024
_MAX_TEXT_CHARS = 100_000

_model: Any | None = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()


def _get_model() -> Any:
    """Load the GGUF exactly once (thread-safe; the loader may download it)."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from models.embed_model.text.core import _load_model

            logger.info("Embedding daemon loading bge-m3 …")
            _model = _load_model()
            logger.info("Embedding daemon model ready.")
    return _model


def _parse_input(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("input")
    if isinstance(raw, str):
        texts = [raw]
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        texts = list(raw)
    else:
        raise ValueError("input must be a string or a list of strings")
    if not texts:
        raise ValueError("input must not be empty")
    limit = int(LOCAL_DAEMONS["max_batch_inputs"])
    if len(texts) > limit:
        raise ValueError(f"input batch exceeds {limit} items")
    if any(len(text) > _MAX_TEXT_CHARS for text in texts):
        raise ValueError(f"an input text exceeds {_MAX_TEXT_CHARS} characters")
    return texts


class _Handler(BaseHTTPRequestHandler):
    watchdog: IdleWatchdog

    def log_message(self, format: str, *args: tuple[object, ...]) -> None:  # noqa: A002
        logger.debug(f"embed daemon: {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        self.watchdog.touch()
        if self.path.rsplit("?", 1)[0].rstrip("/") != _HEALTHY_PATH:
            self.send_error(404)
            return
        ready = _model is not None
        send_json(self, 200, {"status": "ok" if ready else "warming", "ready": ready})

    def do_POST(self) -> None:  # noqa: N802
        self.watchdog.touch()
        if self.path.rsplit("?", 1)[0].rstrip("/") != _EMBED_PATH:
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
            texts = _parse_input(payload)
            model = _get_model()
            with _infer_lock:
                result = model.create_embedding(input=texts)
            data = result.get("data", [])
            if len(data) != len(texts):
                raise RuntimeError(f"model returned {len(data)} vectors for {len(texts)} inputs")
            data.sort(key=lambda item: item["index"])
            send_json(self, 200, {"data": data})
        except Exception as error:  # noqa: BLE001 - one bad batch must not kill the daemon
            logger.exception(f"embed daemon error: {error}")
            send_json(self, 500, {"error": str(error)[:400]})


def main() -> None:
    # Preload in the background so /healthy is reachable while the GGUF maps.
    threading.Thread(target=_get_model, daemon=True).start()
    watchdog = IdleWatchdog(
        float(LOCAL_DAEMONS["embed_idle_timeout_s"]),
        lambda: os._exit(0),
    )
    _Handler.watchdog = watchdog
    watchdog.start()
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    logger.info(f"Embedding daemon listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
