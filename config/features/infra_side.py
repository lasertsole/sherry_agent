"""Infrastructure-side feature configuration registry.

Purpose
-------
One ``TypedDict`` per infrastructure-side feature holding the frozen default
values for every tuneable that the feature currently declares as a module-level
constant or an environment-sourced value. This module is the **single source of
truth**; consumers bind aliases to these fields instead of declaring their own
copies.

The module is deliberately dependency-free: it imports only the standard
library, so it can be consumed from anywhere without a circular import back into
``agent/``, ``server/`` or ``models/``.

Environment-sourced fields (``GATEWAY``) are read when the module-level
instances are built (import time), preserving today's import-time binding
semantics. The private ``_build_gateway`` helper accepts an optional mapping so
tests can inject values.

Several source sites use mutable ``set``/``list`` literals; the registry
normalizes them to ``frozenset``/``tuple`` so instances stay immutable and match
their declared types.
"""

import os
from collections.abc import Mapping
from typing import TypedDict


class GatewayConfig(TypedDict):
    """Gateway bind address (host and port)."""

    api_host: str
    api_port: int


def _build_gateway(env: Mapping[str, str] | None = None) -> GatewayConfig:
    """Build the gateway config, reading ``API_HOST``/``API_PORT`` env vars."""
    source = os.environ if env is None else env
    return GatewayConfig(
        api_host=source.get("API_HOST", "127.0.0.1"),
        api_port=int(source.get("API_PORT", "8080")),
    )


GATEWAY: GatewayConfig = _build_gateway()


class BusConfig(TypedDict):
    """Message-bus bounded-queue configuration."""

    queue_maxsize: int


BUS: BusConfig = {
    "queue_maxsize": 1000,
}


class HttpUploadConfig(TypedDict):
    """HTTP upload size limits, in bytes."""

    max_image_bytes: int
    max_audio_bytes: int
    max_video_bytes: int


HTTP_UPLOAD: HttpUploadConfig = {
    "max_image_bytes": 25 * 1024 * 1024,
    "max_audio_bytes": 100 * 1024 * 1024,
    "max_video_bytes": 500 * 1024 * 1024,
}


class RetryBackoffConfig(TypedDict):
    """Jittered and adaptive retry-backoff defaults."""

    jittered_base_delay: float
    jittered_max_delay: float
    jittered_jitter: float
    backoff_floor: float
    adaptive_base_delay: float
    adaptive_max_delay: float


RETRY_BACKOFF: RetryBackoffConfig = {
    "jittered_base_delay": 2.0,
    "jittered_max_delay": 60.0,
    "jittered_jitter": 0.3,
    "backoff_floor": 0.1,
    "adaptive_base_delay": 5.0,
    "adaptive_max_delay": 120.0,
}


class ServerHttpConfig(TypedDict):
    """HTTP server tunables: log location, media handling, prompts and skills."""

    log_dir: str
    media_dir_name: str
    turn_page_size: int
    audio_content_type_to_ext: dict[str, str]
    audio_default_ext: str
    image_content_type_to_ext: dict[str, str]
    image_default_ext: str
    video_content_type_to_ext: dict[str, str]
    video_default_ext: str
    knowledge_graph_allowed_ext: frozenset[str]
    skills_disk_to_category: dict[str, str]
    skills_skip_dirs: frozenset[str]
    skills_skip_suffixes: frozenset[str]
    subagent_public_fields: tuple[str, ...]
    env_group_prefixes: tuple[str, ...]
    env_split_out_keys: frozenset[str]
    memory_system_file_names: list[str]
    memory_max_content_length: int


SERVER_HTTP: ServerHttpConfig = {
    # Consumers join this suffix under ROOT_DIR to derive the absolute path.
    "log_dir": "/logs/output",
    "media_dir_name": "media",
    "turn_page_size": 200,
    "audio_content_type_to_ext": {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/x-flac": ".flac",
    },
    "audio_default_ext": ".mp3",
    "image_content_type_to_ext": {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    },
    "image_default_ext": ".png",
    "video_content_type_to_ext": {
        "video/mp4": ".mp4",
        "video/mpeg": ".mpeg",
        "video/webm": ".webm",
        "video/ogg": ".ogg",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "video/x-matroska": ".mkv",
    },
    "video_default_ext": ".mp4",
    "knowledge_graph_allowed_ext": frozenset({".pdf", ".docx", ".txt", ".md"}),
    "skills_disk_to_category": {
        "builtin": "builtin",
        "auto": "auto",
        "plugins": "third_party",
    },
    "skills_skip_dirs": frozenset({"__pycache__", ".git", ".venv", "node_modules"}),
    "skills_skip_suffixes": frozenset({".pyc", ".pyo"}),
    "subagent_public_fields": (
        "run_id",
        "child_session_key",
        "requester_session_key",
        "task",
        "task_name",
        "label",
        "spawn_mode",
        "context_mode",
        "agent_id",
        "depth",
        "role",
        "control_scope",
        "generation",
        "swarm_group_id",
        "swarm_run_state",
        "ended_reason",
        "pause_reason",
        "execution",
        "completion",
        "delivery",
    ),
    "env_group_prefixes": (
        "MAIN_LLM_",
        "REASONER_LLM_",
        "AUXILIARY_LLM_",
        "ITTT_",
        "VTTT_",
        "TTI_",
        "RERANKER_",
        "EMBEDDING_",
        "STT_",
    ),
    "env_split_out_keys": frozenset(
        {
            "TOOL_CALL_TIMEOUT_MINUTES",
            "LOG_LEVEL",
            "SUBAGENT_TODO_DONE_FUNC",
            "WORKSPACE_TEMPLATE_LANG",
            "LANGSMITH_TRACING_V2",
            "LANGSMITH_API_KEY",
            "LANGSMITH_PROJECT",
        }
    ),
    "memory_system_file_names": ["MEMORY.md", "USER.md"],
    "memory_max_content_length": 8000,
}


class WsStreamConfig(TypedDict):
    """WebSocket stream retry and flattening tunables."""

    max_continuation_retries: int
    max_reasoning_only_retries: int
    drain_error_backoff_s: float
    max_flatten_depth: int
    stream_diag_headers: tuple[str, ...]


WS_STREAM: WsStreamConfig = {
    "max_continuation_retries": 4,
    "max_reasoning_only_retries": 2,
    "drain_error_backoff_s": 1.0,
    "max_flatten_depth": 5,
    "stream_diag_headers": (
        "cf-ray",
        "cf-cache-status",
        "x-request-id",
        "x-openrouter-provider",
        "x-openrouter-model",
        "server",
        "via",
        "x-vercel-id",
    ),
}


class InputQueueConfig(TypedDict):
    """User-input queue tuning (busy timeout, capacity, expiry, sweeping)."""

    busy_timeout_ms: int
    init_wait_timeout_s: float
    max_active_per_session: int
    expiry_seconds: float
    lock_sweep_threshold: int


INPUT_QUEUE: InputQueueConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "max_active_per_session": 20,
    "expiry_seconds": 86400.0,
    "lock_sweep_threshold": 256,
}


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


class CronServiceConfig(TypedDict):
    """Cron service thresholds and run-history limits."""

    min_every_ms: int
    max_run_history: int
    degraded_threshold: int
    disabled_threshold: int
    degrade_backoff_base_ms: int
    degrade_backoff_max_ms: int
    ws_session_id: str


CRON_SERVICE: CronServiceConfig = {
    "min_every_ms": 1000,
    "max_run_history": 20,
    "degraded_threshold": 5,
    "disabled_threshold": 10,
    "degrade_backoff_base_ms": 5000,
    "degrade_backoff_max_ms": 300000,
    "ws_session_id": "default",
}


class SkillScannerConfig(TypedDict):
    """SkillSpector scanner limits, exit codes and environment flags."""

    cli_timeout: int
    cli_timeout_env_var: str
    version_probe_timeout: int
    cache_version: int
    cache_path: str
    exit_ok: int
    exit_do_not_install: int
    exit_error: int
    fail_closed_on_do_not_install: bool
    enabled_env_var: str
    llm_enabled_env_var: str


SKILL_SCANNER: SkillScannerConfig = {
    "cli_timeout": 120,
    "cli_timeout_env_var": "SKILL_SCANNER_TIMEOUT",
    "version_probe_timeout": 10,
    "cache_version": 1,
    "cache_path": "src/data/skills_scan_cache.json",
    "exit_ok": 0,
    "exit_do_not_install": 1,
    "exit_error": 2,
    "fail_closed_on_do_not_install": True,
    "enabled_env_var": "SKILL_SCANNER_ENABLED",
    "llm_enabled_env_var": "SKILL_SCANNER_LLM",
}


class MesMemoryConfig(TypedDict):
    """MesMemory SQLite connection and FTS query caps."""

    busy_timeout_s: float
    connect_attempts: int
    retry_delay_s: float
    max_query_tokens: int
    max_token_chars: int
    max_wildcard_terms: int


MES_MEMORY: MesMemoryConfig = {
    "busy_timeout_s": 10.0,
    "connect_attempts": 5,
    "retry_delay_s": 0.2,
    "max_query_tokens": 64,
    "max_token_chars": 64,
    "max_wildcard_terms": 4,
}


class CuratorDefaultsConfig(TypedDict):
    """Skill-curator default intervals and retention windows."""

    default_interval_hours: int
    default_min_idle_hours: int
    default_stale_after_days: int
    default_archive_after_days: int
    default_consolidate: bool
    interval_override_min_days: int
    interval_override_max_days: int
    http_interval_min_days: int
    http_interval_max_days: int


CURATOR_DEFAULTS: CuratorDefaultsConfig = {
    "default_interval_hours": 120,
    "default_min_idle_hours": 2,
    "default_stale_after_days": 30,
    "default_archive_after_days": 90,
    "default_consolidate": False,
    "interval_override_min_days": 1,
    "interval_override_max_days": 5,
    "http_interval_min_days": 1,
    "http_interval_max_days": 5,
}


class MessagePipelineConfig(TypedDict):
    """Message-pipeline caps for last-turn slicing and tool-output dedup."""

    slice_last_turn_token_max: int
    tool_output_dedup_default_protected_tools: frozenset[str]


MESSAGE_PIPELINE: MessagePipelineConfig = {
    "slice_last_turn_token_max": 6000,
    "tool_output_dedup_default_protected_tools": frozenset(),
}


class PeriodicBackoffConfig(TypedDict):
    """Periodic-task backoff defaults."""

    factor: float
    max_interval_s: float
    max_consecutive_failures: int


PERIODIC_BACKOFF: PeriodicBackoffConfig = {
    "factor": 2.0,
    "max_interval_s": 7200.0,
    "max_consecutive_failures": 5,
}


class CrashLoopConfig(TypedDict):
    """Crash-loop breaker window, trip threshold and retention."""

    window_s: int
    trip_threshold: int
    retention_s: int
    reason_max_len: int


CRASH_LOOP: CrashLoopConfig = {
    "window_s": 300,
    "trip_threshold": 3,
    "retention_s": 3600,
    "reason_max_len": 200,
}


class SkillsToolingConfig(TypedDict):
    """Skill tooling defaults (speech daemon/server, video, wiki)."""

    speech_daemon_host: str
    speech_daemon_port: int
    speech_ready_wait_seconds: float
    speech_liveness_timeout_seconds: float
    speech_http_timeout_seconds: float
    speech_server_host: str
    speech_server_port: int
    video_min_duration_sec: float
    video_max_duration_sec: float
    skill_creator_max_skill_name_length: int
    wiki_subdir: str


SKILLS_TOOLING: SkillsToolingConfig = {
    "speech_daemon_host": "127.0.0.1",
    "speech_daemon_port": 9011,
    "speech_ready_wait_seconds": 8.0,
    "speech_liveness_timeout_seconds": 1.0,
    "speech_http_timeout_seconds": 60.0,
    "speech_server_host": "127.0.0.1",
    "speech_server_port": 9011,
    "video_min_duration_sec": 0.0,
    "video_max_duration_sec": 60.0,
    "skill_creator_max_skill_name_length": 64,
    "wiki_subdir": "wiki",
}


class ChannelsConfig(TypedDict):
    """Channel dependency-install timeout."""

    dep_install_timeout_seconds: int


CHANNELS: ChannelsConfig = {
    "dep_install_timeout_seconds": 120,
}
