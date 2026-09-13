"""Tool-call truncation boost base/cap/retry settings."""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class MaxTokensBoostConfig(TypedDict):
    """Tool-call truncation boost base/cap/retry settings."""

    base_max_tokens: int
    default_max_tokens: int
    max_cap: int
    max_retries: int


def _build_max_tokens_boost(env: Mapping[str, str] | None = None) -> MaxTokensBoostConfig:
    """Build the max-tokens boost config, reading the env at call time."""
    source = env or os.environ
    return {
        "base_max_tokens": _env_int("MAIN_LLM_OUTPUT_MAX_TOKEN", 8192, source),
        "default_max_tokens": 8192,
        "max_cap": 32_768,
        "max_retries": 3,
    }


MAX_TOKENS_BOOST: MaxTokensBoostConfig = _build_max_tokens_boost()
