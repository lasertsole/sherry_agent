"""Gateway bind address (host and port) and the loopback auth policy."""

import os
from collections.abc import Mapping
from typing import TypedDict


class GatewayConfig(TypedDict):
    """Gateway bind address + the loopback auth policy."""

    api_host: str
    api_port: int
    #: Origins allowed to talk to the gateway: the desktop webview (Tauri) and
    #: the local dev server. A request carrying an Origin outside this list is
    #: refused before any handler runs.
    allowed_origins: list[str]
    #: Strict bearer mode: refuse token-less requests (``/auth/token`` stays
    #: reachable so a pre-provisioned client can still bootstrap).
    require_token: bool


#: Default allowlist: Tauri webview origins (per platform) + the Nuxt dev
#: server on its loopback forms. Override with ``SHERRY_ALLOWED_ORIGINS``
#: (comma-separated), e.g. when the frontend runs on another host/port.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "tauri://localhost",  # macOS / Linux webview
    "http://tauri.localhost",  # Windows webview
    "https://tauri.localhost",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://[::1]:3000",
)


def _build_gateway(env: Mapping[str, str] | None = None) -> GatewayConfig:
    """Build the gateway config, reading ``API_HOST``/``API_PORT`` env vars."""
    source = os.environ if env is None else env
    raw_origins = source.get("SHERRY_ALLOWED_ORIGINS", "").strip()
    origins = (
        [entry.strip() for entry in raw_origins.split(",") if entry.strip()]
        if raw_origins
        else list(DEFAULT_ALLOWED_ORIGINS)
    )
    return GatewayConfig(
        api_host=source.get("API_HOST", "127.0.0.1"),
        api_port=int(source.get("API_PORT", "8080")),
        allowed_origins=origins,
        require_token=source.get("SHERRY_GATEWAY_REQUIRE_TOKEN", "").strip().lower()
        in {"1", "true", "yes", "on"},
    )


GATEWAY: GatewayConfig = _build_gateway()
