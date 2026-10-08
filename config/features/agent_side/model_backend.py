"""Local-vs-remote backend flags for the multimodal understanding models."""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class ModelBackendConfig(TypedDict):
    """Local-vs-remote backend flags for the multimodal understanding models."""

    ittt_model_local: int
    vttt_model_local: int


def _build_model_backend(env: Mapping[str, str] | None = None) -> ModelBackendConfig:
    """Build the model-backend flags, reading the env at call time."""
    source = env or os.environ
    return {
        "ittt_model_local": _env_int("ITTT_MODEL_LOCAL", 0, source),
        "vttt_model_local": _env_int("VTTT_MODEL_LOCAL", 0, source),
    }


MODEL_BACKEND: ModelBackendConfig = _build_model_backend()
