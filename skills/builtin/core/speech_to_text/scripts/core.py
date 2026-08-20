import sys
from pathlib import Path

# Dynamically add project root to sys.path so ``config`` / ``models`` resolve.
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

import requests  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import validate_call  # noqa: E402

from config import INTERPRETER_PATH, ROOT_DIR  # noqa: E402

# The persistent STT daemon holds the FunASR model in memory.
DAEMON_HOST: str = "127.0.0.1"
DAEMON_PORT: int = 9011
_DAEMON_BASE: str = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
# Cold-start grace period: how long a client waits for a freshly spawned daemon
# to become ready. Kept small (well under the terminal's 30s budget) so a first
# launch returns [warm-up] quickly and lets the AI auto-retry instead of
# blocking until the terminal tool kills the whole command.
_READY_WAIT: float = 8.0
_LIVENESS_TIMEOUT: float = 1.0
# Generous HTTP timeout so a /transcribe call never trips the client while the
# already-ready daemon is decoding audio.
_HTTP_TIMEOUT: float = 60.0

# Where ``main`` lives, so the daemon can be (re)spawned from anywhere.
_IMPORT_FN = "skills.builtin.core.speech_to_text.scripts.server"
_INTERPRETER = Path(INTERPRETER_PATH if Path(INTERPRETER_PATH).exists() else ROOT_DIR / ".venv/Scripts/python").as_posix()


def _daemon_alive() -> bool:
    """Return True if the STT daemon is reachable."""
    try:
        r = requests.get(f"{_DAEMON_BASE}/healthy", timeout=_LIVENESS_TIMEOUT)
        return r.status_code == 200 and r.json().get("ready") is True
    except Exception:  # noqa: BLE001
        return False


def _spawn_daemon() -> None:
    """Start the detached STT daemon if it isn't already running.

    Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` keeps the daemon
    alive after this subprocess exits, so model loading survives the terminal
    tool's 30s timeout.
    """
    cmd: list[str] = [_INTERPRETER, "-c", f"from {_IMPORT_FN} import main; main()"]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Redirect output/stderr so no console window is spawned; DETACHED_PROCESS
        # keeps the daemon alive after this subprocess exits, so model loading
        # survives the terminal tool's 30s timeout.
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        logger.info(f"STT daemon spawning (pid={proc.pid}).")
    except Exception as e:  # noqa: BLE001
        logger.error(f"STT daemon spawn failed: {e}")


@validate_call
def stt(audio_path: str) -> str:
    """Transcribe an audio file through the persistent STT daemon.

    Args:
        audio_path: Local absolute path to the audio file (.wav, .mp3, .ogg ...).

    Returns:
        The recognized text, prefixed with a success marker, or an error /
        warm-up notice on failure.
    """
    # Resolve the absolute path early so the transparent url/text checker is not hit.
    p = Path(audio_path)
    if not p.exists():
        return f"[error] audio file not found: {audio_path}"

    if not _daemon_alive():
        logger.info("STT daemon not running; spawning...")
        _spawn_daemon()
        deadline = time.time() + _READY_WAIT
        while time.time() < deadline:
            if _daemon_alive():
                break
            time.sleep(0.5)
        else:
            # Cold start: the FunASR model is still loading in the background
            # daemon (which survives this process's death). Return a fast,
            # actionable [warm-up] so the terminal call stays well under its
            # 30s budget; the AI should retry this same skill command shortly.
            logger.info("STT model still warming up; returning [warm-up].")
            return (
                "[warm-up] The speech-to-text model is cold-starting in a background "
                "daemon for the first time and is not ready yet. Please retry this "
                "exact skill command (same audio path) in a moment, and it will "
                "transcribe once the model finishes loading."
            )

    try:
        resp = requests.post(
            f"{_DAEMON_BASE}/transcribe",
            json={"audio_path": p.as_posix()},
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json()
        if resp.status_code != 200:
            err = data.get("error", f"HTTP {resp.status_code}")
            return f"[Error] Transcription failed: {err}"
        text: str = data.get("text", "")
        suc_mes: str = f"Audio recognition completed, content:\n{text}"
        logger.info(suc_mes)
        return suc_mes
    except Exception as e:  # noqa: BLE001
        err_mes: str = f"[Error] Call failed: {e}"
        logger.error(err_mes)
        return err_mes
