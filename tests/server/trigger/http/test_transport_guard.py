"""Transport gate for the sensitive high-risk ledger.

Same-host or encrypted transports see real data; a plaintext proxied/remote
caller gets a degraded, empty payload (never the ledger's args_preview).
"""

from __future__ import annotations

import pytest

from server.trigger.http.transport_guard import (
    DEGRADED_MESSAGE,
    degraded_response,
    ledger_transport_allowed,
)

pytestmark = [pytest.mark.unit]


class _Req:
    def __init__(self, headers: dict[str, str] | None = None, ip: str | None = None) -> None:
        if headers is not None:
            self.headers = headers
        if ip is not None:
            self.ip_addr = ip


class TestTransportAllowed:
    def test_loopback_peer_is_allowed(self):
        assert ledger_transport_allowed(_Req(ip="127.0.0.1", headers={"host": "x"})) is True

    def test_loopback_host_is_allowed(self):
        assert ledger_transport_allowed(_Req(headers={"host": "localhost:8080"})) is True

    def test_ipv6_loopback_host_is_allowed(self):
        assert ledger_transport_allowed(_Req(headers={"host": "[::1]:8080"})) is True

    def test_forwarded_https_is_allowed(self):
        assert (
            ledger_transport_allowed(
                _Req(headers={"host": "app.example.com", "x-forwarded-proto": "https"})
            )
            is True
        )

    def test_forwarded_wss_is_allowed(self):
        assert (
            ledger_transport_allowed(
                _Req(headers={"host": "app.example.com", "x-forwarded-proto": "wss"})
            )
            is True
        )

    def test_forwarded_chain_with_tls_hop_is_allowed(self):
        assert (
            ledger_transport_allowed(
                _Req(headers={"host": "app.example.com", "x-forwarded-proto": "http, https"})
            )
            is True
        )

    def test_plain_http_remote_is_degraded(self):
        assert (
            ledger_transport_allowed(
                _Req(headers={"host": "app.example.com", "x-forwarded-proto": "http"})
            )
            is False
        )

    def test_proxied_without_proto_is_degraded_even_from_loopback_peer(self):
        # The peer is the proxy (loopback), but XFF marks a proxied remote call.
        assert (
            ledger_transport_allowed(
                _Req(
                    ip="127.0.0.1",
                    headers={"host": "cell:8080", "x-forwarded-for": "203.0.113.7"},
                )
            )
            is False
        )

    def test_remote_peer_without_headers_is_degraded(self):
        assert (
            ledger_transport_allowed(_Req(ip="203.0.113.7", headers={"host": "cell:8080"})) is False
        )

    def test_headerless_in_process_call_is_allowed(self):
        # Unit tests / direct in-process calls cross no network.
        assert ledger_transport_allowed(_Req()) is True

    def test_case_insensitive_header_names(self):
        assert (
            ledger_transport_allowed(
                _Req(headers={"Host": "app.example.com", "X-Forwarded-Proto": "https"})
            )
            is True
        )


class TestDegradedResponse:
    def test_payload_is_empty_and_flagged(self):
        payload = degraded_response()
        assert payload["success"] is True
        assert payload["degraded"] is True
        assert payload["keywords"] == []
        assert payload["ops"] == []
        assert DEGRADED_MESSAGE in payload["message"]
        assert "HTTPS/WSS" in payload["message"]
