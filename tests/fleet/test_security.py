"""JWT claim shape and the active-tenant cookie."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from fleet.config import IngressConfig
from fleet.security import (
    ACTIVE_TENANT_COOKIE,
    claims_tenants,
    clear_auth_cookies,
    issue_tokens,
    set_active_tenant_cookie,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _config(tmp_path: Path) -> IngressConfig:
    return IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
    )


def test_tokens_carry_the_default_first_tenant_list(tmp_path: Path):
    user = {
        "id": 7,
        "username": "alice",
        "role": "user",
        "tenant_id": "team-a",
    }

    access, refresh, access_claims, refresh_claims = issue_tokens(
        user, _config(tmp_path), ["team-b", "team-a"]
    )

    assert access and refresh
    for claims in (access_claims, refresh_claims):
        assert claims["tenants"] == ["team-b", "team-a"]
        assert claims["tenant_id"] == "team-b"
        assert claims["default_tenant"] == "team-b"
    assert claims_tenants(access_claims) == ["team-b", "team-a"]


def test_legacy_scalar_claims_are_still_understood():
    assert claims_tenants({"tenant_id": "team-a"}) == ["team-a"]
    assert claims_tenants({"tenants": ["team-b"]}) == ["team-b"]
    assert claims_tenants({}) == []


def test_active_tenant_cookie_is_http_only_and_lax(tmp_path: Path):
    response = web.Response()
    set_active_tenant_cookie(response, "team-b", _config(tmp_path))

    morsel = response.cookies[ACTIVE_TENANT_COOKIE]
    assert morsel.value == "team-b"
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"
    assert morsel["max-age"] == str(30 * 24 * 3600)


def test_logout_clears_the_active_tenant_selection(tmp_path: Path):
    response = web.Response()
    clear_auth_cookies(response, _config(tmp_path))

    assert ACTIVE_TENANT_COOKIE in response.cookies
    assert response.cookies[ACTIVE_TENANT_COOKIE]["max-age"] == "0"
