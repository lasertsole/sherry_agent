"""Local GGUF model daemons — shared lifecycle helpers.

Import from :mod:`models.daemon.base`; this package exists so the daemon
modules can live next to the models they host without another top-level tree.
"""

from models.daemon.base import (
    DaemonError as DaemonError,
    DaemonSpec as DaemonSpec,
    IdleWatchdog as IdleWatchdog,
    ReadyState as ReadyState,
    ensure_ready as ensure_ready,
    is_ready as is_ready,
    kill as kill,
    pid_alive as pid_alive,
    recorded_pid as recorded_pid,
    register_atexit_kill as register_atexit_kill,
    request_json as request_json,
    send_json as send_json,
    spawn as spawn,
    wait_until_ready as wait_until_ready,
)
