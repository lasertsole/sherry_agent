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


HITL_DEFAULTS: HitlDefaultsConfig = {
    "block_recurrence_limit": 3,
    "default_timeout": 60,
    "default_clarify_timeout": 3600,
    "default_kanban_recurrence_limit": 3,
    "default_mcp_reload_confirm": True,
    "default_destructive_slash_confirm": True,
    "default_description_prefix": "Action requires human approval",
}
