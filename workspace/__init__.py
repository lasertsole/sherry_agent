"""Workspace package."""

CORE_SYSTEM_FILE_NAMES: list[str] = [
    "AGENTS.md",
]

COMMUNITY_SYSTEM_FILE_NAMES: list[str] = [
    "SOUL.md",
    "USER.md",
]

# Memory system files live under workspace/memory/ (not the workspace root) and
# are lazily copied there from the same language template directory on first use.
MEMORY_SYSTEM_FILE_NAMES: list[str] = [
    "FACTS.md",
]

ALL_SYSTEM_FILE_NAMES: list[str] = [
    *CORE_SYSTEM_FILE_NAMES,
    *COMMUNITY_SYSTEM_FILE_NAMES,
]
