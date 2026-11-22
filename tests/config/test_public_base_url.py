"""PUBLIC_BASE_URL: media URLs must use the client-facing origin."""

from __future__ import annotations

import pytest

from config import API_HOST, API_PORT, resolve_public_base_url

pytestmark = [pytest.mark.unit]


def test_an_explicit_public_base_url_wins_and_loses_the_trailing_slash():
    assert (
        resolve_public_base_url({"PUBLIC_BASE_URL": "https://sherry.example.com/"})
        == "https://sherry.example.com"
    )


def test_a_blank_public_base_url_keeps_the_historical_loopback_url():
    assert resolve_public_base_url({"PUBLIC_BASE_URL": "  "}) == f"http://{API_HOST}:{API_PORT}"
