"""Configuration for the bare-metal Ingress."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from config import ROOT_DIR, SRC_DIR


@dataclass(frozen=True)
class IngressConfig:
    """Runtime settings; every value has an environment override."""

    host: str
    port: int
    db_path: Path
    jwt_secret: str
    cookie_name: str = "sherry_ingress"
    access_ttl_s: int = 900
    refresh_ttl_s: int = 7 * 24 * 3600
    allowed_origins: tuple[str, ...] = ()
    secure_cookie: bool = False
    static_dir: Path | None = None
    issuer: str = "sherry-ingress"
    #: Where ``python -m fleet create`` builds per-tenant SHERRY_HOME trees.
    fleet_root: Path = SRC_DIR / "data" / "fleet" / "cells"
    #: Origin the cells use for their own media URLs (the Ingress address).
    public_base_url: str = ""
    #: How long a newly started cell may take to answer /health.
    health_timeout_s: float = 60.0
    #: Optional TLS termination (PEM cert/key); external TLS proxies may omit.
    tls_cert: Path | None = None
    tls_key: Path | None = None
    #: Cell log rotation threshold and supervisor cadence.
    log_max_bytes: int = 10 * 1024 * 1024
    supervise: bool = True
    supervise_interval_s: float = 10.0
    #: Per-cell soft limits applied by the spawned shell (0 = host default).
    #: CPU quotas need a cgroup/systemd wrapper; these bound address space and
    #: open files, which is what a runaway cell usually exhausts first.
    cell_memory_mb: int = 0
    cell_nofile: int = 0
    #: Trust ``X-Forwarded-For`` for the login limiter key (only behind a proxy).
    trust_forwarded: bool = False


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> IngressConfig:
    """Read the Ingress settings (``SHERRY_INGRESS_*``)."""
    source = os.environ if env is None else env
    secret = str(source.get("SHERRY_INGRESS_JWT_SECRET", "")).strip()
    if not secret:
        secret = str(source.get("SHERRY_AUTH_SECRET", "")).strip()
    if not secret:
        # A random per-boot secret is safe but invalidates cookies on restart;
        # a deployment that wants stable sessions sets the env explicitly.
        secret = secrets.token_urlsafe(48)
    raw_origins = str(source.get("SHERRY_INGRESS_ALLOWED_ORIGINS", "")).strip()
    origins = tuple(item.strip() for item in raw_origins.split(",") if item.strip())
    raw_cert = str(source.get("SHERRY_INGRESS_TLS_CERT", "")).strip()
    raw_key = str(source.get("SHERRY_INGRESS_TLS_KEY", "")).strip()
    tls_cert = Path(raw_cert).expanduser().resolve() if raw_cert else None
    tls_key = Path(raw_key).expanduser().resolve() if raw_key else None
    raw_static = str(source.get("SHERRY_INGRESS_STATIC_DIR", "")).strip()
    static_dir = (
        Path(raw_static).expanduser().resolve()
        if raw_static
        else (ROOT_DIR / "client" / ".output" / "public").resolve()
    )
    host = str(source.get("SHERRY_INGRESS_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(str(source.get("SHERRY_INGRESS_PORT", "8081")).strip() or "8081")
    public_base_url = str(source.get("PUBLIC_BASE_URL", "")).strip().rstrip("/")
    valid_cert = tls_cert if tls_cert is not None and tls_cert.is_file() else None
    valid_key = tls_key if tls_key is not None and tls_key.is_file() else None
    return IngressConfig(
        host=host,
        port=port,
        db_path=Path(
            str(source.get("SHERRY_INGRESS_DB", str(SRC_DIR / "data" / "fleet.db"))).strip()
        ).expanduser(),
        jwt_secret=secret,
        allowed_origins=origins,
        secure_cookie=_flag(source.get("SHERRY_INGRESS_COOKIE_SECURE"))
        or (valid_cert is not None and valid_key is not None),
        static_dir=static_dir if static_dir.is_dir() else None,
        fleet_root=Path(
            str(source.get("SHERRY_FLEET_ROOT", str(SRC_DIR / "data" / "fleet" / "cells"))).strip()
        ).expanduser(),
        public_base_url=public_base_url or f"http://{host}:{port}",
        health_timeout_s=float(
            str(source.get("SHERRY_FLEET_HEALTH_TIMEOUT_S", "60")).strip() or "60"
        ),
        tls_cert=valid_cert,
        tls_key=valid_key,
        log_max_bytes=int(
            str(source.get("SHERRY_FLEET_LOG_MAX_BYTES", str(10 * 1024 * 1024))).strip()
            or str(10 * 1024 * 1024)
        ),
        supervise=_flag(source.get("SHERRY_FLEET_SUPERVISE"))
        if "SHERRY_FLEET_SUPERVISE" in source
        else True,
        supervise_interval_s=float(
            str(source.get("SHERRY_FLEET_SUPERVISE_INTERVAL_S", "10")).strip() or "10"
        ),
        cell_memory_mb=int(str(source.get("SHERRY_FLEET_CELL_MEMORY_MB", "0")).strip() or "0"),
        cell_nofile=int(str(source.get("SHERRY_FLEET_CELL_NOFILE", "0")).strip() or "0"),
        trust_forwarded=_flag(source.get("SHERRY_INGRESS_TRUST_FORWARDED")),
    )
