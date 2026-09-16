"""Human-in-the-loop approval defaults."""

from typing import TypedDict


class HitlDefaultsConfig(TypedDict):
    """Human-in-the-loop approval defaults."""

    block_recurrence_limit: int
    default_timeout: int
    default_clarify_timeout: int
    default_kanban_recurrence_limit: int
    default_mcp_reload_confirm: bool
    default_destructive_slash_confirm: bool
    default_description_prefix: str
    yolo_deny_paths: list[str]


HITL_DEFAULTS: HitlDefaultsConfig = {
    "block_recurrence_limit": 3,
    "default_timeout": 60,
    "default_clarify_timeout": 3600,
    "default_kanban_recurrence_limit": 3,
    "default_mcp_reload_confirm": True,
    "default_destructive_slash_confirm": True,
    "default_description_prefix": "Action requires human approval",
    # Security floor for external path access: these paths stay denied even
    # when YOLO is on, the session allowlist matches, or a subagent inherits
    # its parent's authorization. `~` is expanded at check time; a trailing
    # separator means "this directory and everything under it", no separator
    # means exact match. Users extend the list via `yolo_deny_paths` in
    # sherry.jsonc (merged after these defaults, duplicates dropped).
    "yolo_deny_paths": [
        "~/.ssh/",
        "~/.aws/",
        "~/.gnupg/",
        "~/.config/gcloud/",
        "~/.env",
        "~/.gitconfig",
        "~/.npmrc",
        "~/.pypirc",
    ],
}
