"""Shared env→client-kwargs assembly for the model wrappers (DESIGN_PATTERN §5.3).

Every remote model client (main / reasoner / auxiliary / ITTT / VTTT) repeated
the same three steps: read a fixed set of env vars, place them in an
``init_chat_model`` kwargs dict next to model-specific keys, and drop every
``None``/empty-string entry before construction. This module owns that invariant
core. The env *var names* and model-specific extras (temperature / max_retries /
timeout / profile / reasoning) stay at each call site because the repo's naming
is irregular (``ITTT_API_NAME`` vs ``MAIN_LLM_NAME``) and the extras genuinely
differ per model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def read_env(name: str, *, strip: bool = True) -> str | None:
    """Read *name* from the environment, collapsing blank values to ``None``.

    ``strip=False`` returns the raw value (an empty string included) so callers
    that used a plain ``os.getenv`` keep their exact semantics; the empty string
    is still dropped by :func:`clean_client_kwargs` afterwards.
    """
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip() if strip else raw
    return value if value != "" else None


def clean_client_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None``/empty-string entries before ``init_chat_model``."""
    return {key: value for key, value in config.items() if value is not None and value != ""}


class ModelEnvBuilder:
    """Assemble a client kwargs dict from per-model env vars and extra kwargs.

    ``env_vars`` maps each produced key (``model_provider`` / ``model`` /
    ``api_key`` / ``base_url``) to the env var holding it. ``strip=False``
    preserves models that read their env values verbatim (main / reasoner); the
    ``None``/empty filter runs either way.
    """

    __slots__ = ("_env_vars", "_strip")

    def __init__(self, env_vars: Mapping[str, str], *, strip: bool = True) -> None:
        self._env_vars = env_vars
        self._strip = strip

    def build(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        """Read the credentials, merge *extra*, and drop None/empty entries."""
        config: dict[str, Any] = {
            key: read_env(var, strip=self._strip) for key, var in self._env_vars.items()
        }
        config.update(extra)
        return clean_client_kwargs(config)
