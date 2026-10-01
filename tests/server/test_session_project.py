"""P2: the session project-directory binding (service + routes + prompt block).

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
    assert svc.promote_pending_project_dir(SESSION) is True
    assert svc.get_project_state(SESSION).directory == str(other.resolve())
    assert svc.get_project_state(SESSION).pending is None
    assert svc.promote_pending_project_dir(SESSION) is False, "nothing left to promote"


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
