"""Reranker daemon: one warm bge-reranker-v2-m3 GGUF behind loopback HTTP.

The in-process backend loaded the 636 MB GGUF and rebuilt its classifier head on
every call. The daemon keeps one ``CrossEncoderGGUF`` alive, serialises scoring
(llama.cpp is not thread-safe) and lets the orchestrator keep only the scores.

Endpoints (JSON, loopback only):
    GET  /healthy → {"ready": bool}
    POST /rerank  → {query, documents, top_k?, gap_score?} → {"result": [...]}
    POST /filter  → {query, documents, gap_score}         → {"result": [str, ...]}
    POST /predict → {pairs: [[query, passage], ...]}      → {"scores": [float, ...]}
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
PORT: int = LOCAL_DAEMONS["reranker_port"]
_HEALTHY_PATH = "/healthy"
_MAX_BODY_BYTES = 32 * 1024 * 1024
_MAX_DOCUMENTS = 512
_MAX_PAIRS = 512
_MAX_TEXT_CHARS = 50_000

_model: Any | None = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()


def _get_model() -> Any:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            # This child exists only to serve the local reranker, so the
            # process-level switch that guards ``CrossEncoderGGUF`` must be on
            # even when the parent's environment carries the cloud default.
            os.environ["RERANKER_MODEL_LOCAL"] = "true"
            from models.reranker_model.core import CrossEncoderGGUF

            logger.info("Reranker daemon loading bge-reranker-v2-m3 …")
            # ``model_path=None`` resolves the default GGUF (downloading it on
            # first use); ``use_gpu=False`` matches the in-process backend.
            _model = CrossEncoderGGUF(model_path=None, use_gpu=False)
            logger.info("Reranker daemon model ready.")
    return _model


def _texts(raw: Any, *, field: str, limit: int) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be a list of strings")
    if len(raw) > limit:
        raise ValueError(f"{field} exceeds {limit} items")
    if any(len(item) > _MAX_TEXT_CHARS for item in raw):
        raise ValueError(f"an element of {field} exceeds {_MAX_TEXT_CHARS} characters")
    return list(raw)


def _query(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("query must be a non-empty string")
    if len(raw) > _MAX_TEXT_CHARS:
        raise ValueError(f"query exceeds {_MAX_TEXT_CHARS} characters")
    return raw


def _pairs(raw: Any) -> list[tuple[str, str]]:
    if not isinstance(raw, list) or len(raw) > _MAX_PAIRS:
        raise ValueError(f"pairs must be a list of at most {_MAX_PAIRS} pairs")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError("each pair must be [query, passage]")
        if len(item[0]) > _MAX_TEXT_CHARS or len(item[1]) > _MAX_TEXT_CHARS:
            raise ValueError(f"a pair element exceeds {_MAX_TEXT_CHARS} characters")
        pairs.append((item[0], item[1]))
    return pairs


class _Handler(BaseHTTPRequestHandler):
    watchdog: IdleWatchdog

    def log_message(self, format: str, *args: tuple[object, ...]) -> None:  # noqa: A002
        logger.debug(f"reranker daemon: {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        self.watchdog.touch()
        if self.path.rsplit("?", 1)[0].rstrip("/") != _HEALTHY_PATH:
            self.send_error(404)
            return
        ready = _model is not None
        send_json(self, 200, {"status": "ok" if ready else "warming", "ready": ready})

    def do_POST(self) -> None:  # noqa: N802
        self.watchdog.touch()
        path = self.path.rsplit("?", 1)[0].rstrip("/")
        if path not in ("/rerank", "/filter", "/predict"):
            send_json(self, 404, {"error": "not found"})
            return
        try:
            payload = self._read_payload()
            model = _get_model()
            with _infer_lock:
                if path == "/rerank":
                    top_k = payload.get("top_k")
                    if top_k is not None and (
                        isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1
                    ):
                        raise ValueError("top_k must be a positive integer")
                    gap_score = payload.get("gap_score")
                    if gap_score is not None and (
                        isinstance(gap_score, bool) or not isinstance(gap_score, (int, float))
                    ):
                        raise ValueError("gap_score must be a number")
                    result = model.rank(
                        _query(payload.get("query")),
                        _texts(payload.get("documents"), field="documents", limit=_MAX_DOCUMENTS),
                        top_k=top_k,
                        gap_score=gap_score,
                    )
                    send_json(self, 200, {"result": result})
                    return
                if path == "/filter":
                    gap_score = float(payload.get("gap_score", 0.5))
                    result = model.filter(
                        _query(payload.get("query")),
                        _texts(payload.get("documents"), field="documents", limit=_MAX_DOCUMENTS),
                        gap_score=gap_score,
                    )
                    send_json(self, 200, {"result": result})
                    return
                scores = model.predict_scores(_pairs(payload.get("pairs")))
                send_json(self, 200, {"scores": [float(score) for score in scores]})
        except Exception as error:  # noqa: BLE001 - one bad request must not kill the daemon
            logger.exception(f"reranker daemon error: {error}")
            send_json(self, 500, {"error": str(error)[:400]})

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > _MAX_BODY_BYTES:
            raise ValueError("request body is empty or too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload


def main() -> None:
    threading.Thread(target=_get_model, daemon=True).start()
    watchdog = IdleWatchdog(
        float(LOCAL_DAEMONS["reranker_idle_timeout_s"]),
        lambda: os._exit(0),
    )
    _Handler.watchdog = watchdog
    watchdog.start()
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    logger.info(f"Reranker daemon listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
