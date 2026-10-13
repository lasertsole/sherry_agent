"""Pipeline-level gateway auth tests (Robyn ``TestClient``).

``tests/server/trigger/test_gateway_auth.py`` drives the helpers and the
middleware function directly; this file drives the REAL request pipeline
(global before-middlewares → handler → global response headers) so the CORS
allowlist, the middleware order and the ``token`` response header are verified
as the server actually assembles them — no socket needed.
"""

from __future__ import annotations

import pytest

# Aliased: a bare ``TestClient`` name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

from server.trigger.auth import BOOTSTRAP_PATH, TOKEN_HEADER, gateway_token
from server.trigger.core import app

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)


def test_bootstrap_endpoint_serves_the_token_and_its_header():
    response = client.get(BOOTSTRAP_PATH)

    assert response.status_code == 200
    assert response.json() == {"token": gateway_token()}
    # The token header is what the client's ofetch wrapper caches in memory.
    assert response.headers.get(TOKEN_HEADER) == gateway_token()


def test_unlisted_origin_is_refused_by_the_pipeline():
    response = client.get(BOOTSTRAP_PATH, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_allowlisted_origin_passes_with_and_without_the_token():
    without = client.get(BOOTSTRAP_PATH, headers={"Origin": "tauri://localhost"})
    assert without.status_code == 200

    with_token = client.get(
        BOOTSTRAP_PATH, headers={"Origin": "tauri://localhost", TOKEN_HEADER: gateway_token()}
    )
    assert with_token.status_code == 200


def test_wrong_token_is_refused_even_from_the_webview_origin():
    response = client.get(BOOTSTRAP_PATH, headers={TOKEN_HEADER: "not-the-token"})

    assert response.status_code == 401


def test_origin_less_requests_are_allowed_for_local_tooling():
    response = client.get(BOOTSTRAP_PATH)

    assert response.status_code == 200
