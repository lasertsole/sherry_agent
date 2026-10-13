"""Security response headers are set on every response (X1 + X2 + X3).

The pipeline test is the important one: it asserts the headers on a response the
*real* Robyn app produced, which is what proves they were installed globally
instead of merely being defined in a module.
"""

from __future__ import annotations

import pytest

# Aliased: a bare ``TestClient`` name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

from config.features import GATEWAY
from server.trigger.auth import BOOTSTRAP_PATH
from server.trigger.core import app
from server.trigger.security_headers import DEFAULT_CSP, security_headers

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)
_MUTATING_PATH = "/sessions/access_mode"


class TestPolicy:
    def test_the_csp_forbids_inline_script_and_plugins(self):
        csp = security_headers()["Content-Security-Policy"]

        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_the_csp_keeps_the_grants_this_app_needs(self):
        """Vue inline styles, upload data URLs and the loopback WS gateway."""
        csp = security_headers()["Content-Security-Policy"]

        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "img-src 'self' data: blob:" in csp
        assert "ws://127.0.0.1:*" in csp and "ws://[::1]:*" in csp

    def test_nosniff_and_framing_headers_are_present(self):
        headers = security_headers()

        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_the_csp_can_be_overridden(self, monkeypatch):
        monkeypatch.setitem(GATEWAY, "csp", "default-src 'none'")

        assert security_headers()["Content-Security-Policy"] == "default-src 'none'"

    def test_the_csp_can_be_disabled(self, monkeypatch):
        monkeypatch.setitem(GATEWAY, "csp", "disabled")

        headers = security_headers()
        assert "Content-Security-Policy" not in headers
        assert headers["X-Content-Type-Options"] == "nosniff", "the other headers stay"

    def test_an_empty_override_falls_back_to_the_shipped_policy(self, monkeypatch):
        monkeypatch.setitem(GATEWAY, "csp", "")

        assert security_headers()["Content-Security-Policy"] == DEFAULT_CSP


class TestPipeline:
    def test_a_real_response_carries_the_headers(self):
        response = client.get(BOOTSTRAP_PATH)

        assert response.headers.get("Content-Security-Policy") == DEFAULT_CSP
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_a_refused_request_carries_them_too(self):
        """A 403 our middleware produced is still a response a browser renders.

        The CSRF refusal is used as the sample (a mutating request with a
        cross-site fetch metadata header): it is produced by our own code, so
        the headers asserted here are the ones we attach, not the CORS layer's.
        """
        response = client.put(
            _MUTATING_PATH,
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
            body="{}",
        )

        assert response.status_code == 403
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Content-Security-Policy") == DEFAULT_CSP
