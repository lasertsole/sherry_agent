"""Ingress configuration env resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.config import load_config

pytestmark = [pytest.mark.unit]


def test_tls_files_enable_secure_cookies(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    config = load_config(
        {
            "SHERRY_INGRESS_TLS_CERT": str(cert),
            "SHERRY_INGRESS_TLS_KEY": str(key),
            "PUBLIC_BASE_URL": "https://sherry.example.com/",
            "SHERRY_INGRESS_JWT_SECRET": "s" * 40,
        }
    )

    assert config.tls_cert == cert
    assert config.tls_key == key
    assert config.secure_cookie is True
    assert config.public_base_url == "https://sherry.example.com"


def test_missing_tls_files_are_ignored(tmp_path: Path):
    config = load_config(
        {
            "SHERRY_INGRESS_TLS_CERT": str(tmp_path / "missing.pem"),
            "SHERRY_INGRESS_TLS_KEY": str(tmp_path / "missing.key"),
            "SHERRY_INGRESS_JWT_SECRET": "s" * 40,
        }
    )

    assert config.tls_cert is None
    assert config.tls_key is None
    assert config.secure_cookie is False


def test_cell_resource_limits_and_supervisor_switches_read_env():
    config = load_config(
        {
            "SHERRY_INGRESS_JWT_SECRET": "s" * 40,
            "SHERRY_FLEET_CELL_MEMORY_MB": "512",
            "SHERRY_FLEET_CELL_NOFILE": "2048",
            "SHERRY_FLEET_SUPERVISE": "0",
        }
    )

    assert config.cell_memory_mb == 512
    assert config.cell_nofile == 2048
    assert config.supervise is False


def test_forwarded_for_is_opt_in():
    assert load_config({"SHERRY_INGRESS_JWT_SECRET": "s" * 40}).trust_forwarded is False
    assert (
        load_config(
            {
                "SHERRY_INGRESS_JWT_SECRET": "s" * 40,
                "SHERRY_INGRESS_TRUST_FORWARDED": "1",
            }
        ).trust_forwarded
        is True
    )
