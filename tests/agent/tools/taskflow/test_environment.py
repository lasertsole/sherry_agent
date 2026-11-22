"""WorkMode stage 5: plan/environment dual-file.

Pins four links in the environment injection chain:
1. taskflow_create(environment=...) persists ``state["environment"]`` verbatim
   (credential refs stay refs, never resolved); no environment -> state unchanged.
2. store_sqlite.get_flow_environment_sync reads it back by flow_id alone,
   failing open to None on missing/malformed data.
3. dispatch_child prefers an explicit environment, else reads the owning flow,
   and forwards it to spawn_subagent_direct (fail-open on read errors).
4. SpawnRequest.from_legacy accepts the environment kwarg like any other field.
"""

from __future__ import annotations

import importlib

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_create import taskflow_create

pytestmark = [pytest.mark.unit]

_SESSION = "sess-env"
_ENV = {
    "project_dir": "/repo/sherry_agent",
    "stack": "python3.13",
    "constraints": ["no network", "read-only"],
    "api_token": {"credential_ref": "keychain://svc/api-token"},
}

dispatch_module = importlib.import_module("agent.tools.taskflow.tools._dispatch")


class _Accepted:
    status = "accepted"
    child_session_key = "agent:main:subagent:child-env"
    error = ""


# ---------------------------------------------------------------------------
# 1. create persists environment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persists_environment_verbatim(isolated_db):
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-1",
        description="env probe",
        environment=_ENV,
    )
    assert "Error" not in out, out
    flow = await store_sqlite.get_flow("flow-env-1", _SESSION)
    assert flow is not None
    assert flow["state"]["environment"] == _ENV
    # credential ref is stored as a ref, never resolved to a secret.
    assert flow["state"]["environment"]["api_token"] == {
        "credential_ref": "keychain://svc/api-token"
    }


@pytest.mark.asyncio
async def test_create_without_environment_leaves_state_unchanged(isolated_db):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-plain",
        description="no env",
    )
    flow = await store_sqlite.get_flow("flow-env-plain", _SESSION)
    assert "environment" not in flow["state"]


@pytest.mark.asyncio
async def test_empty_environment_not_persisted(isolated_db):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-empty",
        description="empty env",
        environment={},
    )
    flow = await store_sqlite.get_flow("flow-env-empty", _SESSION)
    assert "environment" not in flow["state"]


# ---------------------------------------------------------------------------
# 2. sync read by flow_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_read_environment_by_flow_id(isolated_db):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-sync",
        description="sync read",
        environment=_ENV,
    )
    assert store_sqlite.get_flow_environment_sync("flow-env-sync") == _ENV


def test_sync_read_missing_flow_returns_none(isolated_db):
    assert store_sqlite.get_flow_environment_sync("does-not-exist") is None


@pytest.mark.asyncio
async def test_sync_read_malformed_environment_returns_none(isolated_db, monkeypatch):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-bad",
        description="malformed",
        environment=_ENV,
    )
    # Corrupt state so environment is no longer a dict -> must fail open.
    flow = await store_sqlite.get_flow("flow-env-bad", _SESSION)
    state = flow["state"]
    state["environment"] = "not-a-dict"
    await store_sqlite.update_flow(
        "flow-env-bad",
        flow["expected_revision"],
        session_id=_SESSION,
        state=state,
    )
    assert store_sqlite.get_flow_environment_sync("flow-env-bad") is None


# ---------------------------------------------------------------------------
# 3. dispatch forwards environment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_forwards_explicit_environment(
    isolated_db, monkeypatch, real_child_dispatch
):
    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return _Accepted()

    monkeypatch.setattr("agent.tools.subagent.spawn_subagent_direct", _fake_spawn)

    key = await real_child_dispatch(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
        flow_id="flow-env-explicit",
        environment=_ENV,
    )
    assert key == _Accepted.child_session_key
    assert captured["environment"] == _ENV


@pytest.mark.asyncio
async def test_dispatch_reads_flow_environment_when_not_explicit(
    isolated_db, monkeypatch, real_child_dispatch
):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-from-store",
        description="from store",
        environment=_ENV,
    )
    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return _Accepted()

    monkeypatch.setattr("agent.tools.subagent.spawn_subagent_direct", _fake_spawn)
    await real_child_dispatch(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
        flow_id="flow-env-from-store",
    )
    assert captured["environment"] == _ENV


@pytest.mark.asyncio
async def test_dispatch_explicit_environment_wins_over_flow(
    isolated_db, monkeypatch, real_child_dispatch
):
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-env-override",
        description="override",
        environment=_ENV,
    )
    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return _Accepted()

    monkeypatch.setattr("agent.tools.subagent.spawn_subagent_direct", _fake_spawn)
    override = {"project_dir": "/override"}
    await real_child_dispatch(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
        flow_id="flow-env-override",
        environment=override,
    )
    assert captured["environment"] == override


@pytest.mark.asyncio
async def test_dispatch_no_environment_passes_none(isolated_db, monkeypatch, real_child_dispatch):
    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return _Accepted()

    monkeypatch.setattr("agent.tools.subagent.spawn_subagent_direct", _fake_spawn)
    await real_child_dispatch(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
    )
    assert captured["environment"] is None


@pytest.mark.asyncio
async def test_dispatch_read_error_fails_open(isolated_db, monkeypatch, real_child_dispatch):
    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return _Accepted()

    monkeypatch.setattr("agent.tools.subagent.spawn_subagent_direct", _fake_spawn)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(store_sqlite, "get_flow_environment_sync", _boom)
    await real_child_dispatch(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
        flow_id="flow-env-readfail",
    )
    # A failed read must not block dispatch; the child just has no env block.
    assert captured["environment"] is None


# ---------------------------------------------------------------------------
# 4. SpawnRequest accepts environment through the legacy kwarg shape
# ---------------------------------------------------------------------------


def test_spawn_request_from_legacy_accepts_environment():
    from agent.tools.subagent.spawn.request import SpawnRequest

    req = SpawnRequest.from_legacy(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
        environment=_ENV,
    )
    assert req.environment == _ENV
    assert req.as_kwargs()["environment"] == _ENV


def test_spawn_request_default_environment_is_none():
    from agent.tools.subagent.spawn.request import SpawnRequest

    req = SpawnRequest.from_legacy(
        task="do work",
        requester_session_key=f"agent:main:{_SESSION}",
    )
    assert req.environment is None
