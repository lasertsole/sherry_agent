"""HTTP server tunables: log location, media handling, prompts and skills."""

from typing import TypedDict


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
