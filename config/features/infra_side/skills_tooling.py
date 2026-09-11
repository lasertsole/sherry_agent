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
