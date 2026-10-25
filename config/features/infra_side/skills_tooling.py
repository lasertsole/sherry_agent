"""Skill tooling defaults (speech daemon/server, video, wiki)."""

from typing import TypedDict


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
    code_wiki_export_max_symbols: int
    code_wiki_export_max_modules: int
    code_wiki_export_max_diagram_nodes: int
    code_wiki_export_max_class_edges: int
    wiki_lint_low_confidence_ratio: float


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
    # Deterministic code_wiki export (code_intel SQLite → markdown).
    "code_wiki_export_max_symbols": 5000,
    "code_wiki_export_max_modules": 12,
    "code_wiki_export_max_diagram_nodes": 50,
    "code_wiki_export_max_class_edges": 40,
    # A wiki page whose edges are mostly INFERRED is flagged for human review.
    "wiki_lint_low_confidence_ratio": 0.7,
}
