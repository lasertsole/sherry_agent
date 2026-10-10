"""Boot-time reconciliation of session folders against the store records."""

from typing import TypedDict


class SessionDirsConfig(TypedDict):
    """Boot-time reconciliation of session folders against the store records."""

    #: Run the orphan scan at boot (it only removes folders NO store knows).
    boot_sweep_enabled: bool
    #: Never scan a folder younger than this — a session may still be initializing.
    min_dir_age_seconds: int


SESSION_DIRS: SessionDirsConfig = {
    "boot_sweep_enabled": True,
    "min_dir_age_seconds": 300,
}
