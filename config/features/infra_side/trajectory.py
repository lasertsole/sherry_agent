"""Per-turn trajectory ledger: an event timeline per turn, for the observability pane."""

from typing import TypedDict


class TrajectoryConfig(TypedDict):
    """Per-turn trajectory ledger: an event timeline per turn, for the observability pane."""

    enabled: bool
    db_path: str
    max_turns_per_session: int
    payload_max_chars: int
    result_clip_chars: int


TRAJECTORY: TrajectoryConfig = {
    #: The ledger is cheap (3-12 rows per turn) and always useful — on by default.
    "enabled": True,
    #: Empty = ``SESSIONS_DIR/trajectory.db`` (beside the session folders).
    "db_path": "",
    #: Keep the newest N turns per session; older turns are pruned on write.
    "max_turns_per_session": 50,
    #: ``payload_json`` cap (chars) — the full result already lives in messages.
    "payload_max_chars": 64000,
    #: Cap for the one-line ``summary`` column.
    "result_clip_chars": 2000,
}
