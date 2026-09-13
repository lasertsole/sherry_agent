"""Gateway bind address (host and port)."""

import os
from collections.abc import Mapping
from typing import TypedDict


class GatewayConfig(TypedDict):
    """Gateway bind address (host and port)."""

    api_host: str
    api_port: int


def _build_gateway(env: Mapping[str, str] | None = None) -> GatewayConfig:
    """Build the gateway config, reading ``API_HOST``/``API_PORT`` env vars."""
    source = os.environ if env is None else env
    return GatewayConfig(
        api_host=source.get("API_HOST", "127.0.0.1"),
        api_port=int(source.get("API_PORT", "8080")),
    )


GATEWAY: GatewayConfig = _build_gateway()
