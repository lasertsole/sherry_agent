"""Heartbeat service defaults (session, file, interval, backoff)."""

from typing import TypedDict


class HeartbeatServiceConfig(TypedDict):
    """Heartbeat service defaults (session, file, interval, backoff)."""

    ws_session_id: str
    heartbeat_file_name: str
    max_content_length: int
    interval_s: int
    backoff_factor: float
    backoff_max_interval_s: float
    backoff_max_consecutive_failures: int
    main_llm_env_vars: tuple[str, ...]


HEARTBEAT_SERVICE: HeartbeatServiceConfig = {
    "ws_session_id": "default",
    "heartbeat_file_name": "HEARTBEAT.md",
    "max_content_length": 2000,
    "interval_s": 1800,
    "backoff_factor": 2.0,
    "backoff_max_interval_s": 7200.0,
    "backoff_max_consecutive_failures": 5,
    "main_llm_env_vars": (
        "MAIN_LLM_PROVIDER",
        "MAIN_LLM_NAME",
        "MAIN_LLM_API_BASE",
        "MAIN_LLM_API_KEY",
        "MAIN_LLM_MAX_TOKEN",
        "MAIN_LLM_ENABLE_THINKING",
        "MAIN_LLM_REASONING_EFFORT",
    ),
}
