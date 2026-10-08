"""SSRF guard unit tests for :mod:`pub.func.validator.public_url`.

The guard resolves a URL's host and accepts it only when every resolved
address is globally routable: loopback, RFC1918, link-local (cloud metadata)
and mixed public+private answers are refused, and a resolution failure is a
refusal too (fail-closed). ``SHERRY_ALLOW_PRIVATE_MEDIA_URLS=1`` is the
documented local-development escape hatch.
"""

from __future__ import annotations

import socket

import pytest

from pub.func.validator import is_public_url
from pub.func.validator import public_url as guard_mod

pytestmark = [pytest.mark.unit]


def _patch_dns(monkeypatch, *addresses: str) -> None:
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]
    monkeypatch.setattr(guard_mod.socket, "getaddrinfo", lambda *args, **kwargs: infos)


def _patch_dns_failure(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(guard_mod.socket, "getaddrinfo", _boom)


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", raising=False)


def test_public_host_is_accepted(monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34")
    assert is_public_url("https://example.com/a.png") is True


def test_loopback_is_refused(monkeypatch):
    _patch_dns(monkeypatch, "127.0.0.1")
    assert is_public_url("http://localhost:8080/x") is False


def test_rfc1918_is_refused(monkeypatch):
    _patch_dns(monkeypatch, "192.168.1.10")
    assert is_public_url("http://nas.lan/media/a.mp3") is False


def test_cloud_metadata_link_local_is_refused(monkeypatch):
    _patch_dns(monkeypatch, "169.254.169.254")
    assert is_public_url("http://metadata.internal/latest/meta-data/") is False


def test_mixed_public_and_private_answers_are_refused(monkeypatch):
    """DNS round-robin must not smuggle the guard."""
    _patch_dns(monkeypatch, "93.184.216.34", "10.0.0.7")
    assert is_public_url("https://rebind.example/a.png") is False


def test_ipv4_mapped_ipv6_loopback_is_refused(monkeypatch):
    _patch_dns(monkeypatch, "::ffff:127.0.0.1")
    assert is_public_url("http://mapped.example/a.png") is False


def test_resolution_failure_is_refused(monkeypatch):
    _patch_dns_failure(monkeypatch)
    assert is_public_url("https://no-such-host.invalid/a.png") is False


def test_non_http_scheme_is_refused():
    assert is_public_url("file:///etc/passwd") is False
    assert is_public_url("ftp://example.com/a.mp3") is False


def test_empty_and_missing_host_are_refused():
    assert is_public_url("") is False
    assert is_public_url("http:///nohost") is False


def test_escape_hatch_allows_private_targets(monkeypatch):
    _patch_dns(monkeypatch, "127.0.0.1")
    monkeypatch.setenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "1")
    assert is_public_url("http://127.0.0.1:9000/a.mp3") is True
