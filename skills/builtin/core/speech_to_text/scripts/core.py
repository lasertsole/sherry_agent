import sys
from pathlib import Path

# Dynamically add project root to sys.path so ``config`` / ``models`` resolve.
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import time  # noqa: E402

import requests  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import validate_call  # noqa: E402

from config import APP_HOME  # noqa: E402
from config.features import SKILLS_TOOLING  # noqa: E402
from models.daemon.base import (  # noqa: E402
    DaemonSpec,
    is_ready,
    kill,
    pid_alive,
    recorded_pid,
    spawn,
)

# The persistent STT daemon holds the FunASR model in memory.
DAEMON_HOST: str = SKILLS_TOOLING["speech_daemon_host"]
DAEMON_PORT: int = SKILLS_TOOLING["speech_daemon_port"]
_DAEMON_BASE: str = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
# Cold-start grace period: how long a client waits for a freshly spawned daemon
# to become ready. Kept small (well under the terminal's 30s budget) so a first
# launch returns [warm-up] quickly and lets the AI auto-retry instead of
# blocking until the terminal tool kills the whole command.
_READY_WAIT: float = SKILLS_TOOLING["speech_ready_wait_seconds"]
_LIVENESS_TIMEOUT: float = SKILLS_TOOLING["speech_liveness_timeout_seconds"]
# Generous HTTP timeout so a /transcribe call never trips the client while the
# already-ready daemon is decoding audio.
_HTTP_TIMEOUT: float = SKILLS_TOOLING["speech_http_timeout_seconds"]

# Where ``main`` lives, so the daemon can be (re)spawned from anywhere.
_IMPORT_FN = "skills.builtin.core.speech_to_text.scripts.server"

# The pid file lives under APP_HOME (writable in a packaged desktop build; the
# code root is read-only there). A checkout keeps the historical location.
_STT_SPEC = DaemonSpec(
    name="STT",
    host=DAEMON_HOST,
    port=DAEMON_PORT,
    pid_path=APP_HOME / ".stt_daemon.pid",
    import_fn=_IMPORT_FN,
    ready_wait_s=_READY_WAIT,
    liveness_timeout_s=_LIVENESS_TIMEOUT,
)
_PID_PATH: Path = _STT_SPEC.pid_path


def _daemon_alive() -> bool:
    """Return True if the STT daemon is reachable."""
    return is_ready(_STT_SPEC)


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _recorded_daemon_pid() -> int | None:
    return recorded_pid(_STT_SPEC)


def _spawn_daemon() -> None:
    """Start the detached STT daemon if it isn't already running.

    The detached-process flags and the interpreter selection live in
    :mod:`models.daemon.base`, shared with the local model daemons.
    """
    spawn(_STT_SPEC)


def _kill_daemon() -> None:
    """Terminate the recorded STT daemon (public alias below)."""
    kill(_STT_SPEC)


# Public aliases for server-side reuse (server/service/audio_transcribe_service.py):
# the browser voice-input proxy drives the SAME daemon lifecycle (probe / spawn /
# wait) instead of a second spawn implementation, so the pid file and the
# detached-process semantics stay defined in exactly one place.
daemon_alive = _daemon_alive
spawn_daemon = _spawn_daemon
kill_daemon = _kill_daemon
DAEMON_BASE = _DAEMON_BASE
READY_WAIT = _READY_WAIT
HTTP_TIMEOUT = _HTTP_TIMEOUT


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
        logger.exception(err_mes)
        return err_mes
