"""Workspace package."""

CORE_SYSTEM_FILE_NAMES: list[str] = [
    "AGENTS.md",
]

COMMUNITY_SYSTEM_FILE_NAMES: list[str] = [
    "SOUL.md",
    "USER.md",
    # The role statement — who the AI plays, who the user plays ("用户将扮演…").
    # A persona file like SOUL.md by design: it enters the system prompt through
    # the same frozen per-session snapshot, rides the same workspace presets,
    # and is re-composed in the active UI language by the 预设 panel.
    "ROLE.md",
]

ALL_SYSTEM_FILE_NAMES: list[str] = [
    *CORE_SYSTEM_FILE_NAMES,
    *COMMUNITY_SYSTEM_FILE_NAMES,
]
