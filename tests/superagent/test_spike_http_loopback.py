"""P-Spike 3: gateway-authenticated HTTP loopback in both directions.

A side process can POST to Sherry with the gateway token; Sherry can invoke a
registered outbound callback from inside the same handler. Real socket binding
is a P2 concern — this spike proves the seam and its auth contract without
opening a port.
"""

from __future__ import annotations

import pytest

# Aliased: a bare TestClient name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

from server.trigger.auth import TOKEN_HEADER, gateway_token
from server.trigger.core import app
from server.trigger.http import superagent as superagent_http

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)


def test_side_process_can_post_with_gateway_token() -> None:
    response = client.post(
        "/internal/test",
        json_data={"echo": "ping"},
        headers={TOKEN_HEADER: gateway_token()},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["echo"] == "ping"


def test_loopback_route_rejects_a_wrong_gateway_token() -> None:
    # Origin-less local tooling may omit the token by design; a PRESENT but
    # invalid token must still be refused by the global gateway middleware.
    response = client.post(
        "/internal/test",
        json_data={"echo": "ping"},
        headers={TOKEN_HEADER: "not-the-token"},
    )

    assert response.status_code == 401


def test_sherry_can_invoke_the_reverse_direction_hook() -> None:
    calls = []

    def _hook(payload):
        calls.append(payload)
        return {"ok": True, "seen": payload["payload"]["echo"]}

    superagent_http.set_outbound_hook(_hook)
    try:
        response = client.post(
            "/internal/test",
            json_data={"echo": "pong"},
            headers={TOKEN_HEADER: gateway_token()},
        )
    finally:
        superagent_http.set_outbound_hook(None)

    assert response.status_code == 200
    assert calls == [{"kind": "test", "payload": {"echo": "pong"}}]
    assert response.json()["outbound"] == {"ok": True, "seen": "pong"}
