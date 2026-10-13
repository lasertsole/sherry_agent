"""CSRF guard for state-changing requests (C1 + C2 of the hardening plan).

Two layers of evidence, like the gateway-auth suite next door: the verdict
matrix drives ``csrf_verdict`` directly (every signal combination the policy
distinguishes), and the pipeline tests drive the real Robyn request pipeline so
the middleware's registration and its response shape are checked where they
actually run.
"""

from __future__ import annotations

import json

import pytest

# Aliased: a bare ``TestClient`` name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

from config.features import GATEWAY
from server.trigger.auth import BOOTSTRAP_PATH
from server.trigger.core import app
from server.trigger.csrf import csrf_verdict

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)

#: A mutating route that exists on the shipped app; its handler may reject the
#: body, which is fine — these tests only distinguish "refused by the guard"
#: (403 with the guard's message) from "reached the handler".
_MUTATING_PATH = "/sessions/access_mode"


class TestVerdictMatrix:
    def test_read_methods_are_never_gated(self):
        """A cross-site GET is normal web traffic; gating it would break assets."""
        assert csrf_verdict("GET", "https://evil.example", None, "cross-site") is None
        assert csrf_verdict("HEAD", None, None, "cross-site") is None
        assert csrf_verdict("OPTIONS", None, None, "cross-site") is None

    def test_cross_site_sec_fetch_site_is_refused_even_without_origin(self):
        """The signal a hostile page cannot forge, and the one a simple form
        POST can send while omitting Origin."""
        verdict = csrf_verdict("POST", None, None, "cross-site")

        assert verdict is not None
        assert verdict[0] == 403

    def test_same_origin_and_same_site_pass(self):
        assert csrf_verdict("POST", "tauri://localhost", None, "same-origin") is None
        assert csrf_verdict("PUT", "tauri://localhost", None, "same-site") is None

    def test_loopback_origin_passes(self):
        assert csrf_verdict("PATCH", "http://127.0.0.1:3000", None, None) is None
        assert csrf_verdict("DELETE", "http://localhost:3000", None, None) is None
        assert csrf_verdict("POST", "http://[::1]:3000", None, None) is None

    def test_allowlisted_tauri_origin_passes(self):
        assert csrf_verdict("POST", "https://tauri.localhost", None, None) is None

    def test_foreign_origin_is_refused(self):
        verdict = csrf_verdict("POST", "https://evil.example/x", None, None)

        assert verdict is not None and verdict[0] == 403

    def test_foreign_referer_is_refused_when_origin_is_absent(self):
        verdict = csrf_verdict("POST", None, "https://evil.example/page", None)

        assert verdict is not None and verdict[0] == 403

    def test_loopback_referer_passes(self):
        assert csrf_verdict("POST", None, "http://127.0.0.1:3000/chat", None) is None

    def test_script_client_without_browser_headers_passes(self):
        """curl / pytest / the desktop shell send none of the three headers.

        Carrying the token in strict mode, or the Origin allowlist, is what
        authenticates them — the CSRF layer must not turn automation into 403s.
        """
        assert csrf_verdict("POST", None, None, None) is None

    def test_null_origin_is_refused(self):
        """A sandboxed iframe or a file:// page sends ``Origin: null``."""
        verdict = csrf_verdict("POST", "null", None, None)

        assert verdict is not None and verdict[0] == 403


class TestPipeline:
    def test_a_cross_site_mutation_is_refused_with_a_json_body(self):
        response = client.put(
            _MUTATING_PATH,
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
            body=json.dumps({"session_id": "s-csrf", "mode": "yolo"}),
        )

        assert response.status_code == 403
        body = response.json()
        assert body["success"] is False
        assert "CSRF" in body["message"]

    def test_an_absent_origin_from_a_script_still_reaches_the_handler(self):
        """The guard has to be invisible to non-browser clients."""
        response = client.put(
            _MUTATING_PATH,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"session_id": "s-csrf", "mode": "yolo"}),
        )

        assert response.status_code != 403

    def test_a_cross_site_read_is_not_gated(self):
        response = client.get(BOOTSTRAP_PATH, headers={"Sec-Fetch-Site": "cross-site"})

        assert response.status_code == 200

    def test_the_switch_disables_the_guard(self, monkeypatch):
        monkeypatch.setitem(GATEWAY, "csrf_guard_enabled", False)

        response = client.put(
            _MUTATING_PATH,
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
            body=json.dumps({"session_id": "s-csrf", "mode": "yolo"}),
        )

        assert response.status_code != 403, "the documented switch must bypass the guard"
