"""TLS termination context (the Ingress serves HTTPS when cert+key exist)."""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fleet.__main__ import build_ssl_context
from fleet.config import IngressConfig

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _write_cert(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_a_valid_cert_and_key_build_a_server_context(tmp_path: Path):
    cert, key = _write_cert(tmp_path)
    config = IngressConfig(
        host="127.0.0.1",
        port=8443,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        tls_cert=cert,
        tls_key=key,
    )

    context = build_ssl_context(config)

    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_without_tls_files_the_ingress_stays_plain_http(tmp_path: Path):
    config = IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
    )

    assert build_ssl_context(config) is None
