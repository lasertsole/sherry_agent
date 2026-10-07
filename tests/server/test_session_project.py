"""The session project-directory binding (service + routes + prompt block).

Contract:

- nothing bound -> the process default applies and ``source`` says so;
- ``PUT /sessions/project`` validates (absolute / existing / directory), stores
  the session's own value, and two sessions never see each other's root;
- ``{"directory": null}`` clears the binding back to the process default;
- a rejected value leaves the stored state untouched;
- the prompt block names the effective root, and marks an unbound session with
  the 未绑定项目目录 warning instead of silently pointing at the checkout.

The register is redirected at the durable tier (``state_register_db``) so the
tests never touch the real ``state_register.db``; mem is cleared around each.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import server.trigger.http.session_project as project_api
from runtime.session.project_dir import read_project_dir_durable
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem
from server.service import session_project_service as svc

pytestmark = [pytest.mark.unit]

SESSION = "sess-project-1"


class _FakeRequest:
    """Minimal request stand-in for the route handlers."""

    def __init__(self, payload=None, query: dict | None = None) -> None:
        self._payload = payload
        self.query_params = query or {}

    def json(self):
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


def _call(handler, request: _FakeRequest):
    return asyncio.run(handler(request))


def _payload(response) -> dict:
    return json.loads(response.description)


class _InMemoryRegisterDB:
    """Dict-backed stand-in for the durable tier (never touches any real DB)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.store.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.store[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.store.pop((session_id, key), None)
        return True

    def get_all_session_ids(self) -> list[str]:
        """Distinct sessions present in the mirror (used by the boot priming)."""
        return sorted({sid for sid, _key in self.store})


@pytest.fixture(autouse=True)
def _isolated_register(monkeypatch):
    """Swap the durable tier for an in-memory store and clear the mem tier."""
    import runtime

    monkeypatch.setattr(runtime, "state_register_db", _InMemoryRegisterDB())
    for sid in (SESSION, "sess-project-2"):
        state_register_mem.clear_session(sid)
    yield
    for sid in (SESSION, "sess-project-2"):
        state_register_mem.clear_session(sid)

    # Guard: if a code path ever imported the durable register under a name this
    # patch does not reach, the write would land in the REAL state_register.db.
    # Fail loudly instead of silently polluting runtime state (this actually
    # happened while writing these tests: the readers used
    # ``from runtime.session import state_register_db``, which the established
    # ``runtime.state_register_db`` patch point does not intercept).
    import sqlite3

    from config import SRC_DIR

    real_db = SRC_DIR / "data" / "state_register.db"
    if real_db.exists():
        conn = sqlite3.connect(real_db)
        try:
            leaked = conn.execute(
                "SELECT COUNT(*) FROM states WHERE session_id LIKE 'sess-project%'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert leaked == 0, "tests wrote to the real state_register.db (patch point missed)"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    target = tmp_path / "proj"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_unbound_session_falls_back_to_the_process_default(monkeypatch):
    monkeypatch.delenv("SHERRY_PROJECT_DIR", raising=False)

    state = svc.get_project_state(SESSION)

    assert state.directory is None
    assert state.source == "default"
    from config import ROOT_DIR

    assert state.effective == str(ROOT_DIR)


def test_binding_wins_over_the_process_default(project: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(tmp_path / "other"))

    state = svc.apply_project_choice(SESSION, str(project))

    assert state.directory == str(project.resolve())
    assert state.effective == str(project.resolve())
    assert state.source == "session"
    # And it round-trips through a fresh read (durable mirror).
    state_register_mem.clear_session(SESSION)
    assert svc.get_project_state(SESSION).directory == str(project.resolve())


def test_env_default_reports_the_env_source(tmp_path: Path, monkeypatch):
    other = tmp_path / "env-dir"
    other.mkdir()
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(other))

    state = svc.get_project_state(SESSION)

    assert state.directory is None
    assert state.source == "env"
    assert state.effective == str(other.resolve())


def test_sessions_are_isolated(project: Path, tmp_path: Path):
    other = tmp_path / "other-proj"
    other.mkdir()
    svc.apply_project_choice(SESSION, str(project))
    svc.apply_project_choice("sess-project-2", str(other))

    assert svc.get_project_state(SESSION).directory == str(project.resolve())
    assert svc.get_project_state("sess-project-2").directory == str(other.resolve())
    state_register_mem.clear_session("sess-project-2")


def test_clearing_restores_the_process_default(project: Path):
    svc.apply_project_choice(SESSION, str(project))

    state = svc.apply_project_choice(SESSION, None)

    assert state.directory is None
    assert state.source in {"env", "default"}
    assert svc.get_project_state(SESSION).directory is None


@pytest.mark.parametrize("bad", ["relative/dir", "/nonexistent/definitely-missing"])
def test_rejected_values_leave_the_state_untouched(project: Path, bad: str):
    svc.apply_project_choice(SESSION, str(project))

    with pytest.raises(ValueError):
        svc.apply_project_choice(SESSION, bad)

    assert svc.get_project_state(SESSION).directory == str(project.resolve())


def test_rejects_a_file(project: Path):
    target = project / "f.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        svc.apply_project_choice(SESSION, str(target))


def test_rejects_an_invalid_session_id(project: Path):
    with pytest.raises(ValueError):
        svc.apply_project_choice("", str(project))
    with pytest.raises(ValueError):
        svc.get_project_state("../escape")


def test_pending_choice_is_reported_and_promoted(project: Path, tmp_path: Path):
    other = tmp_path / "next"
    other.mkdir()
    svc.apply_project_choice(SESSION, str(project))

    parked = svc.park_project_choice(SESSION, str(other))

    assert parked.pending == str(other.resolve())
    assert svc.get_project_state(SESSION).directory == str(project.resolve()), (
        "live value unchanged"
    )
    assert svc.promote_pending_project_dir(SESSION) == str(other.resolve())
    assert svc.get_project_state(SESSION).directory == str(other.resolve())
    assert svc.get_project_state(SESSION).pending is None
    assert svc.promote_pending_project_dir(SESSION) is None, "nothing left to promote"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_get_reports_the_binding(project: Path):
    svc.apply_project_choice(SESSION, str(project))

    resp = _call(
        project_api.get_project_directory_handler, _FakeRequest(query={"session_id": SESSION})
    )

    assert resp.status_code == 200
    body = _payload(resp)
    assert body["directory"] == str(project.resolve())
    assert body["source"] == "session"
    assert body["pending_directory"] is None


def test_get_requires_a_session_id():
    resp = _call(project_api.get_project_directory_handler, _FakeRequest(query={}))

    assert resp.status_code == 400
    assert "session_id" in _payload(resp)["message"]


def test_put_binds_and_reports(project: Path):
    resp = _call(
        project_api.put_project_directory_handler,
        _FakeRequest(payload={"session_id": SESSION, "directory": str(project)}),
    )

    assert resp.status_code == 200
    body = _payload(resp)
    assert body["ok"] is True
    assert body["directory"] == str(project.resolve())
    assert body["pending"] is False


def test_put_rejects_a_relative_path():
    resp = _call(
        project_api.put_project_directory_handler,
        _FakeRequest(payload={"session_id": SESSION, "directory": "relative/dir"}),
    )

    assert resp.status_code == 400
    assert "absolute" in _payload(resp)["message"]


def test_put_requires_the_directory_key():
    resp = _call(
        project_api.put_project_directory_handler, _FakeRequest(payload={"session_id": SESSION})
    )

    assert resp.status_code == 400
    assert "directory" in _payload(resp)["message"]


def test_put_rejects_a_non_string_directory():
    resp = _call(
        project_api.put_project_directory_handler,
        _FakeRequest(payload={"session_id": SESSION, "directory": 42}),
    )

    assert resp.status_code == 400


def test_put_null_clears(project: Path):
    svc.apply_project_choice(SESSION, str(project))

    resp = _call(
        project_api.put_project_directory_handler,
        _FakeRequest(payload={"session_id": SESSION, "directory": None}),
    )

    assert resp.status_code == 200
    assert _payload(resp)["directory"] is None


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------


def test_prompt_block_names_a_bound_directory(project: Path):
    from workspace.prompt_builder import _build_project_dir_block

    svc.apply_project_choice(SESSION, str(project))

    block = _build_project_dir_block(SESSION)

    assert "## Current Working Directory" in block
    assert str(project.resolve()) in block
    assert "未绑定" not in block


def test_prompt_block_warns_when_unbound(monkeypatch):
    from workspace.prompt_builder import _build_project_dir_block

    monkeypatch.delenv("SHERRY_PROJECT_DIR", raising=False)

    block = _build_project_dir_block(SESSION)

    assert "未绑定项目目录" in block
    assert "default" in block


def test_prompt_injection_includes_the_block(project: Path, monkeypatch):
    from workspace import prompt_builder

    svc.apply_project_choice(SESSION, str(project))
    monkeypatch.setattr(prompt_builder, "get_skills_text", lambda *a, **k: "")
    monkeypatch.setattr(prompt_builder, "_read_static_files", lambda names: [])
    monkeypatch.setattr(prompt_builder, "_read_todos_sync", lambda sid: [])

    prompt = prompt_builder.build_system_prompt(session_id=SESSION)

    assert "## Current Working Directory" in prompt
    assert str(project.resolve()) in prompt


# ---------------------------------------------------------------------------
# Dynamic switching — park while a turn runs, promote at the boundary
# ---------------------------------------------------------------------------


class TestDynamicSwitch:
    @pytest.fixture()
    def other(self, tmp_path: Path) -> Path:
        target = tmp_path / "other-proj"
        target.mkdir()
        return target

    @pytest.mark.asyncio
    async def test_idle_switch_applies_immediately(self, project: Path, other: Path, monkeypatch):
        # The service asks the settings module for the busy verdict (one shared
        # definition of "busy"); stub that seam.
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: False,
            raising=False,
        )

        state = await svc.apply_project_choice_async(SESSION, str(other))

        assert state.pending is None
        assert state.directory == str(other.resolve())
        assert svc.get_project_state(SESSION).directory == str(other.resolve())

    @pytest.mark.asyncio
    async def test_busy_switch_parks_and_the_turn_keeps_reading_the_old_root(
        self, project: Path, other: Path, monkeypatch
    ):
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: True,
            raising=False,
        )
        svc.apply_project_choice(SESSION, str(project))

        state = await svc.apply_project_choice_async(SESSION, str(other))

        assert state.directory == str(project.resolve()), "live value unchanged mid-turn"
        assert state.pending == str(other.resolve()), "the new value is parked"
        from runtime.session.project_dir import current_project_dir

        assert current_project_dir(SESSION) == project.resolve(), "tools still read the old root"

    @pytest.mark.asyncio
    async def test_promotion_lands_at_the_turn_boundary(
        self, project: Path, other: Path, monkeypatch
    ):
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: True,
            raising=False,
        )
        svc.apply_project_choice(SESSION, str(project))
        await svc.apply_project_choice_async(SESSION, str(other))

        assert svc.promote_pending_project_dir(SESSION) == str(other.resolve())

        assert svc.get_project_state(SESSION).directory == str(other.resolve())
        assert svc.get_project_state(SESSION).pending is None

    @pytest.mark.asyncio
    async def test_a_live_write_supersedes_a_parked_choice(
        self, project: Path, other: Path, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: True,
            raising=False,
        )
        await svc.apply_project_choice_async(SESSION, str(other))

        third = tmp_path / "third"
        third.mkdir()
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: False,
            raising=False,
        )
        state = await svc.apply_project_choice_async(SESSION, str(third))

        assert state.directory == str(third.resolve())
        assert state.pending is None, "the parked value was superseded"
        assert svc.promote_pending_project_dir(SESSION) is None

    @pytest.mark.asyncio
    async def test_rejected_value_parks_nothing(self, project: Path, monkeypatch):
        monkeypatch.setattr(
            "server.service.session_settings_service._session_turn_active",
            lambda _sid: True,
            raising=False,
        )
        svc.apply_project_choice(SESSION, str(project))

        with pytest.raises(ValueError):
            await svc.apply_project_choice_async(SESSION, "relative/nope")

        state = svc.get_project_state(SESSION)
        assert state.pending is None
        assert state.directory == str(project.resolve())


def test_turn_runner_promotes_the_parked_directory(monkeypatch):
    """The turn-end hook promotes the parked choice (same seam as the controls)."""
    import asyncio as _asyncio

    import server.service.turn_runner as turn_runner
    from runtime.session import project_dir as project_dir_mod

    calls: list[str] = []

    def fake_promote(session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(turn_runner, "_promote_pending_project_dir", fake_promote)
    monkeypatch.setattr(turn_runner, "is_hitl_pending", lambda _sid: False)
    monkeypatch.setattr(
        turn_runner, "promote_pending_settings", lambda _sid: _asyncio.sleep(0, result=[])
    )

    _asyncio.run(turn_runner.on_turn_finished("sess-project-1"))

    assert calls == ["sess-project-1"]
    assert project_dir_mod is not None


# ---------------------------------------------------------------------------
# Boot priming (the restart-survival fix found by the smoke)
# ---------------------------------------------------------------------------


def test_prime_mem_from_store_warms_bindings(monkeypatch):
    """After a restart the mem tier is empty while the mirror holds the bindings.

    The agent-side readers are mem-only, so an unprimed tier silently served the
    process default (the repo) even though ``GET /sessions/project`` reported the
    real binding. Boot now primes mem from the store.
    """
    import runtime
    from runtime.session import project_dir as project_dir_mod

    fake = _InMemoryRegisterDB()
    fake.set_state(SESSION, str(StateKey.PROJECT_DIR), "/tmp/from-store")
    monkeypatch.setattr(runtime, "state_register_db", fake)
    state_register_mem.clear_session(SESSION)

    warmed = project_dir_mod.prime_mem_from_store()

    assert warmed == 1
    assert project_dir_mod.read_project_dir(SESSION) == Path("/tmp/from-store")
    # Idempotent: a second pass finds nothing to do.
    assert project_dir_mod.prime_mem_from_store() == 0


def test_prime_mem_from_store_ignores_blank_values(monkeypatch):
    import runtime
    from runtime.session import project_dir as project_dir_mod

    fake = _InMemoryRegisterDB()
    fake.set_state(SESSION, str(StateKey.PROJECT_DIR), "   ")
    monkeypatch.setattr(runtime, "state_register_db", fake)
    state_register_mem.clear_session(SESSION)

    assert project_dir_mod.prime_mem_from_store() == 0
    assert project_dir_mod.read_project_dir(SESSION) is None


# ---------------------------------------------------------------------------
# The switch is NOT announced by the service (the notice moved to send time)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_switch_only_binds_and_leaves_the_notice_to_the_middleware(
    project: Path, monkeypatch
):
    """A switch must not inject anything by itself.

    The agent is told at SEND time, by ``WorkspaceNoticeMiddleware``: it
    compares the live root against ``StateKey.PROJECT_DIR_ANNOUNCED`` and
    splices one notice in front of the next user message. A switch-time
    announcement here would double the notice (and fire once per switch, with
    no comparison at all).
    """
    monkeypatch.setattr(
        "server.service.session_settings_service._session_turn_active",
        lambda _sid: False,
        raising=False,
    )

    await svc.apply_project_choice_async(SESSION, str(project))

    assert read_project_dir_durable(SESSION) == Path(str(project)).resolve()
    assert state_register_mem.get_state(SESSION, StateKey.PROJECT_DIR_ANNOUNCED, None) is None, (
        "the notice baseline belongs to the middleware, never to the switch"
    )


def test_the_service_no_longer_exposes_a_switch_time_announcement():
    """Guard the removal: nothing may reintroduce a per-switch transcript write."""
    assert not hasattr(svc, "announce_project_switch")


def test_turn_runner_promotes_a_parked_choice(monkeypatch):
    """The boundary promotion stays; it is silent by design."""
    import asyncio as _asyncio

    import server.service.turn_runner as turn_runner

    promoted: list[str] = []
    monkeypatch.setattr(
        turn_runner,
        "_promote_pending_project_dir",
        lambda session_id: promoted.append(session_id) or "/tmp/new-root",
    )
    monkeypatch.setattr(turn_runner, "is_hitl_pending", lambda _sid: False)
    monkeypatch.setattr(
        turn_runner, "promote_pending_settings", lambda _sid: _asyncio.sleep(0, result=[])
    )

    _asyncio.run(turn_runner.on_turn_finished(SESSION))

    assert promoted == [SESSION], "the parked directory is promoted at the turn boundary"
