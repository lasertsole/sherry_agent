"""Standalone Speech-to-Text daemon.

Loads the FunASR SenseVoice model **once** at startup and serves a fast local
HTTP endpoint. Skill scripts (run via the ``terminal`` tool) call this endpoint
instead of reloading the model in a fresh subprocess, so recognition completes
well within any terminal timeout.

Endpoints (all JSON):
    GET  /healthy   -> {"status": "ok"} once the model is loaded
    POST /transcribe -> {"audio_path": "..."} -> {"text": "..."}

Run: ``python -c "from skills.builtin.core.speech_to_text.scripts.server import main; main()"``
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Put the project root on sys.path so ``config`` / ``models`` import cleanly.
_current_file = Path(__file__).resolve()
_PROJECT_ROOT: Path = _current_file.parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import threading  # noqa: E402  (imports below rely on sys.path above)

import torch  # noqa: E402
from funasr import AutoModel  # noqa: E402
from funasr.utils.postprocess_utils import rich_transcription_postprocess  # noqa: E402
from loguru import logger  # noqa: E402

from config import MODELS_DIR  # noqa: E402

HOST: str = "127.0.0.1"
PORT: int = 9011
_HEALTH_PATH: str = "/healthy"
_TRANSCRIBE_PATH: str = "/transcribe"

_model_dir = MODELS_DIR / "STT_model"

# Model load happens at import time. Guarded by a lock so the /healthy handler
# can safely probe readiness while another thread already owns model loading.
_model_lock = threading.Lock()
_model: AutoModel | None = None


def _ensure_model() -> AutoModel:
    """Load the model exactly once (thread-safe)."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            _device: str = "cuda:0" if torch.cuda.is_available() else "cpu"
            logger.info(f"STT daemon loading model on device: {_device}")
            _model = AutoModel(
                model=(_model_dir / "model_weight").as_posix(),
                trust_remote_code=True,
                remote_code=(_model_dir / "core.py").as_posix(),
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=_device,
            )
            logger.info("STT daemon model ready.")
    return _model


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: tuple[object, ...]) -> None:  # noqa: A002
        logger.debug(f"STT daemon: {format % args}")

    def do_GET(self):  # noqa: N802
        if self.path.rsplit("?", 1)[0].rstrip("/") == _HEALTH_PATH:
            ready: bool = _model is not None
            body = json.dumps({"status": "ok" if ready else "warming", "ready": ready}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        path = self.path.rsplit("?", 1)[0].rstrip("/")
        if path != _TRANSCRIBE_PATH:
            self._reply(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            audio_path = payload.get("audio_path")
            if not audio_path:
                self._reply(400, {"error": "audio_path is required"})
                return
            if not Path(audio_path).exists():
                self._reply(404, {"error": f"audio file not found: {audio_path}"})
                return

            model = _ensure_model()
            res = model.generate(
                input=audio_path,
                cache={},
                language="auto",
                use_itn=True,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )
            text = rich_transcription_postprocess(res[0]["text"])
            self._reply(200, {"text": text})
        except Exception as e:  # noqa: BLE001
            logger.error(f"STT daemon error: {e}")
            self._reply(500, {"error": str(e)})

    def _reply(self, code: int, data: dict[str, object]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    # Kick off model loading in the background so /healthy can report on
    # readiness without blocking the main thread before the server binds.
    threading.Thread(target=_ensure_model, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), _Handler)
    logger.info(f"STT daemon listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
