"""Log retention: age cutoff, total-size cap and the sweep interval."""

from typing import TypedDict


class LogsConfig(TypedDict):
    """Log retention: age cutoff, total-size cap and the sweep interval."""

    #: Files older than this many days are deleted by every sweep (0 disables
    #: the age rule; the size cap below still applies).
    retention_days: int
    #: When the whole log tree exceeds this many MiB, the OLDEST files are
    #: deleted until it fits (0 disables the cap). Actively-written files
    #: (mtime inside the quiescence window) are never touched.
    max_total_mb: int
    #: Seconds between background sweeps (0 = boot-time sweep only). The boot
    #: sweep itself always runs.
    sweep_interval_s: int


LOGS: LogsConfig = {
    "retention_days": 7,
    "max_total_mb": 512,
    "sweep_interval_s": 86400,
}
