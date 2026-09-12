"""Pre-compression Memory Flush configuration (P0-1).

Controls the optional fact-extraction pass that runs before the summarization
middleware discards messages: a cheap model scans the about-to-be-dropped
history and the extracted facts are appended to MEMORY.md.
"""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class MemoryFlushConfig(TypedDict):
    """Pre-compression Memory Flush knobs (single source of truth)."""

    enabled: bool
    model: str
    soft_threshold_tokens: int
    force_flush_chars: int
    output_max_tokens: int
    timeout_seconds: int


def _build_memory_flush(env: Mapping[str, str] | None = None) -> MemoryFlushConfig:
    """Build the memory-flush config, reading the env at call time."""
    source = env or os.environ
    return {
        "enabled": bool(_env_int("MEMORY_FLUSH_ENABLED", 1, source)),
        "model": source.get("MEMORY_FLUSH_MODEL", ""),
        "soft_threshold_tokens": 8_000,
        "force_flush_chars": 50_000,
        "output_max_tokens": 2_048,
        "timeout_seconds": 30,
    }


MEMORY_FLUSH: MemoryFlushConfig = _build_memory_flush()
