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
    #: CSRF guard for mutating methods (POST/PUT/PATCH/DELETE): Sec-Fetch-Site
    #: plus the Origin/Referer loopback check (see server/trigger/csrf.py).
    #: Disable only for a headless client that mutates without any of those
    #: headers while strict token mode carries the authentication.
    csrf_guard_enabled: bool
    #: Content-Security-Policy override: empty uses the shipped policy,
    #: "disabled" omits the header, anything else is used verbatim.
    csp: str


#: Default allowlist: the Tauri webview origins ONLY. The Nuxt dev server
#: origins are deliberately NOT defaults (2026-11-09 audit M4): a local page or
#: a compromised dev server on :3000 could otherwise read the gateway token
#: from /auth/token and drive the API. Development launchers (`start.sh` and
#: the Tauri checkout auto-start) export ``SHERRY_ALLOWED_ORIGINS`` with the
#: dev origins; a deployment that needs another origin sets the same variable.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "tauri://localhost",  # macOS / Linux webview
    "http://tauri.localhost",  # Windows webview
    "https://tauri.localhost",
)

#: The dev-server origins a development launcher may add explicitly. Kept in
#: one place so `start.sh` / the Rust auto-start and the docs cannot drift.
DEV_ALLOWED_ORIGINS: tuple[str, ...] = (
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
        csrf_guard_enabled=source.get("SHERRY_CSRF_GUARD", "1").strip().lower()
        in {"1", "true", "yes", "on"},
        csp=source.get("SHERRY_CSP", "").strip(),
    )


GATEWAY: GatewayConfig = _build_gateway()
