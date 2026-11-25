"""High-risk guard panel API: keyword CRUD + ledger deletion."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.tools.pub_base.high_risk_guard_store import default_store
from server.trigger.http import high_risk_guard as api

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SESSION = "sess-high-risk-api"


class _FakeRequest:
    def __init__(self, body: dict | None = None) -> None:
        self._body = body or {}

    def json(self) -> dict:
        return self._body


def _body(response) -> dict[str, Any]:
    return json.loads(response.description)


def _paths() -> dict[str, str]:
    return {"session_id": SESSION}


@pytest.fixture(autouse=True)
def _guard_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(tmp_path / "guard.db"))


@pytest.mark.asyncio
async def test_add_list_and_delete_a_keyword():
    added = await api.high_risk_keywords_add_handler(
        _FakeRequest({"keyword": "deploy_prod"}), _paths()
    )
    assert added.status_code == 200, _body(added)

    listed = _body(await api.high_risk_keywords_list_handler(None, _paths()))
    entry = next(row for row in listed["keywords"] if row["keyword"] == "deploy_prod")
    assert entry["policy"] == "block"
    assert entry["source"] == "user"

    deleted = await api.high_risk_keywords_delete_handler(
        None, {**_paths(), "keyword_id": str(entry["id"])}
    )
    assert _body(deleted)["success"] is True
    listed_after = _body(await api.high_risk_keywords_list_handler(None, _paths()))
    assert all(row["keyword"] != "deploy_prod" for row in listed_after["keywords"])


@pytest.mark.asyncio
async def test_a_short_keyword_is_refused_with_the_rule():
    response = await api.high_risk_keywords_add_handler(_FakeRequest({"keyword": "ab"}), _paths())
    assert response.status_code == 400
    assert "3" in _body(response)["message"]


@pytest.mark.asyncio
async def test_policy_switch_removes_the_keyword_from_the_blockers():
    await api.high_risk_keywords_add_handler(_FakeRequest({"keyword": "password"}), _paths())
    assert "password" in default_store().load_active_blockers(SESSION)

    response = await api.high_risk_keywords_policy_handler(
        _FakeRequest({"keyword": "password", "policy": "allow"}), _paths()
    )
    assert response.status_code == 200
    assert "password" not in default_store().load_active_blockers(SESSION)


@pytest.mark.asyncio
async def test_reset_clears_the_executed_marker():
    default_store().mark_executed(SESSION, "deploy", "deployed")
    response = await api.high_risk_keywords_reset_handler(
        _FakeRequest({"keyword": "deploy"}), _paths()
    )
    assert _body(response)["success"] is True
    assert default_store().get_keyword(SESSION, "deploy")["executed"] is False


@pytest.mark.asyncio
async def test_delete_a_ledger_operation():
    default_store().record_op(SESSION, "mcp__stripe__charge", {"amount": 1}, "charged")
    op = default_store().list_ops(SESSION)[0]
    response = await api.high_risk_ops_delete_handler(None, {**_paths(), "op_id": str(op["id"])})
    assert _body(response)["success"] is True
    assert default_store().list_ops(SESSION) == []


@pytest.mark.asyncio
async def test_builtin_keywords_are_read_only():
    response = await api.high_risk_builtin_keywords_handler(None)
    keywords = _body(response)["keywords"]
    assert "rm -rf" in keywords and "password" in keywords


class _RemoteRequest(_FakeRequest):
    """A non-loopback caller without encryption: must be degraded."""

    def __init__(self, body: dict | None = None) -> None:
        super().__init__(body)
        self.headers = {"host": "app.example.com", "x-forwarded-proto": "http"}
        self.ip_addr = "203.0.113.7"


@pytest.mark.asyncio
async def test_ledger_list_is_degraded_on_plain_remote_transport():
    default_store().record_op(SESSION, "mcp__stripe__charge", {"amount": 999}, "charged")
    response = await api.high_risk_keywords_list_handler(_RemoteRequest(), _paths())
    assert response.status_code == 200
    body = _body(response)
    assert body["degraded"] is True
    assert body["ops"] == []
    assert body["keywords"] == []
    assert "HTTPS/WSS" in body["message"]


@pytest.mark.asyncio
async def test_ledger_write_is_degraded_on_plain_remote_transport():
    response = await api.high_risk_keywords_add_handler(
        _RemoteRequest({"keyword": "sensitive_operation"}), _paths()
    )
    assert response.status_code == 200
    assert _body(response)["degraded"] is True
    # Nothing was written.
    assert all(
        row["keyword"] != "sensitive_operation" for row in default_store().list_keywords(SESSION)
    )


@pytest.mark.asyncio
async def test_encrypted_remote_transport_sees_real_ledger():
    default_store().record_op(SESSION, "mcp__safe__read", {"k": "v"}, "ok")
    request = _RemoteRequest()
    request.headers = {"host": "app.example.com", "x-forwarded-proto": "https"}
    response = await api.high_risk_keywords_list_handler(request, _paths())
    body = _body(response)
    assert body.get("degraded") is not True
    assert any(op["tool_name"] == "mcp__safe__read" for op in body["ops"])
