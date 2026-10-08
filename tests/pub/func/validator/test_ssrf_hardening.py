"""SSRF verdict hardening: the ranges a fake-ip stack (and an attacker) can use.

``ipaddress``' own predicates leave the RFC 2544 benchmark block and IPv6 unique
local addresses classified as global, so a guard built on them alone lets
``198.18.0.1`` through — the range sing-box / Clash answer *every* public name
with. Both are refused here, and the network-level test drives the real media
handler to show the refusal is model-visible.
"""

from __future__ import annotations

import socket

import pytest

from pub.func.validator import public_url
from pub.func.validator.public_url import (
    _is_global_address,
    is_public_url,
    public_ip_for,
    resolve_public_addresses,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", raising=False)


def _patch_dns(monkeypatch, *addresses: str) -> None:
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]
    monkeypatch.setattr(public_url.socket, "getaddrinfo", lambda *args, **kwargs: infos)


@pytest.mark.parametrize(
    "address",
    ["198.18.0.1", "198.19.255.254", "fd00::1", "fc00::abcd", "::ffff:198.18.0.1"],
)
def test_fake_ip_and_ula_ranges_are_not_global(address):
    assert _is_global_address(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700::1111", "198.20.0.1"])
def test_ordinary_public_addresses_stay_global(address):
    assert _is_global_address(address) is True


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0"]
)
def test_private_and_metadata_addresses_stay_refused(address):
    assert _is_global_address(address) is False


def test_a_fake_ip_answer_refuses_the_public_name(monkeypatch):
    """The documented trade-off: a fake-ip host refuses ordinary URLs too."""
    _patch_dns(monkeypatch, "198.18.0.43")

    assert is_public_url("https://example.com") is False
    assert resolve_public_addresses("example.com") == []
    assert public_ip_for("example.com") is None


def test_a_mixed_answer_yields_no_addresses(monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34", "127.0.0.1")

    assert resolve_public_addresses("example.test") == []


def test_a_public_answer_yields_the_addresses_in_order(monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34", "93.184.216.35")

    assert resolve_public_addresses("example.test") == ["93.184.216.34", "93.184.216.35"]
    assert public_ip_for("example.test") == "93.184.216.34"


def test_a_resolution_failure_is_fail_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(public_url.socket, "getaddrinfo", boom)

    assert resolve_public_addresses("nope.invalid") == []
    assert is_public_url("http://nope.invalid/") is False


def test_the_escape_hatch_pins_nothing_but_allows_the_host(monkeypatch):
    monkeypatch.setenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "1")

    assert is_public_url("http://127.0.0.1:9000/media") is True
    # Pinning is skipped under the hatch: the hostname is dialed as-is.
    assert public_ip_for("127.0.0.1") is None


@pytest.mark.parametrize(
    "url", ["ws://example.test/x", "data:text/plain,hi", "javascript:alert(1)", ""]
)
def test_only_http_and_https_are_accepted(url):
    assert is_public_url(url) is False
