"""Outbound HTTP client trust settings for the cloud model clients."""

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int


class HttpClientConfig(TypedDict):
    """Outbound HTTP client trust settings."""

    #: 1 = verify TLS certificates (default), 0 = skip verification.
    verify_tls: int
    #: Env var read for that flag.
    verify_tls_env_var: str


def _build_http_client(env: Mapping[str, str] | None = None) -> HttpClientConfig:
    """Build the trust settings, reading the env at call time."""
    env_var = "SHERRY_HTTP_VERIFY_TLS"
    source = env or os.environ
    return {"verify_tls": _env_int(env_var, 1, source), "verify_tls_env_var": env_var}


HTTP_CLIENT: HttpClientConfig = _build_http_client()
